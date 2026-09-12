"""
Oracle-1001 / Sentinel — Advanced Quant Mathematical Pipeline
TTF Forecast Accuracy Enhancement Module (Target: >80% directional accuracy)

Five Core Mathematical Models:
  1. BOGDecayEngine      — Boil-Off Gas & Cargo Volume Decay (thermodynamic model)
  2. HMMDestinationPredictor — Bayesian HMM vessel rerouting probability
  3. HydrodynamicsETAEngine  — NOAA current vectors + wave resistance → True ETA
  4. TerminalBottleneckIndex  — Regasification queue clustering (EU hubs)
  5. ICEMicrostructureEnsemble — Physical flow × ICE orderbook → CatBoost signal

Author: Oracle-1001 SRE Quant Desk
Standard: ISO 80000-5 Thermodynamics · IMO LNG Standards · NOAA OSCAR Current Atlas
"""

from __future__ import annotations

import json
import math
import sqlite3
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
OUT  = ROOT / "output"

# ─────────────────────────────────────────────────────────────────────────────
# BOG: Boil-Off Gas & Cargo Volume Decay
# ─────────────────────────────────────────────────────────────────────────────

# IMO MEPC standards for LNG insulation systems (fraction lost per day at STP)
BOG_RATES = {
    "membrane":           0.0015,   # Gaztransport & Technigaz (GTT) Mark III Flex
    "membrane_flex_plus": 0.0012,   # GTT Mark III Flex+ (enhanced insulation)
    "moss_sphere":        0.0010,   # Kvaerner Moss Sphere type (double hull)
    "sph_b":              0.0008,   # SPB (Self-supporting Prismatic type B) — Q-Flex
    "hi_q":               0.0005,   # Latest HI-Q Membrane (ultra-low BOG, 2022+)
}
# Active re-liquefaction plant reduces effective BOG
RELIQ_EFFICIENCY = {
    "full_reliq":  0.70,   # Full re-liquefaction plant (large Q-Max)
    "partial":     0.45,   # Partial re-liquefaction (typical mid-size)
    "none":        0.00,   # No re-liquefaction (older vessels)
}
# Q-Max nominal cargo volume m³ → approx
QMAX_CARGO_M3 = 266_000
QFLEX_CARGO_M3 = 216_000
CONVENTIONAL_CARGO_M3 = 145_000

# LNG energy density at -162°C: ~21.1 MJ/kg, density ~450 kg/m³
LNG_ENERGY_MJ_PER_M3 = 21.1 * 450.0  # ≈ 9495 MJ/m³
MWH_PER_M3_LNG = LNG_ENERGY_MJ_PER_M3 / 3600.0  # ≈ 2.637 MWh/m³


class BOGDecayEngine:
    """
    Thermodynamic BOG decay model for LNG cargo.

    Delivered Volume:
        V_del = V_init × (1 − BOG_rate_effective)^Δt_days

    where BOG_rate_effective = BOG_rate_nominal × (1 − re_liquefaction_efficiency)

    Energy lost to TTF equivalent:
        ΔE = (V_init − V_del) × LNG_MWh_per_m3 × TTF_spot
    """

    def __init__(self, ttf_spot: float = 35.0):
        self.ttf_spot = ttf_spot

    def estimate_cargo_m3(self, dwt_tons: float, vessel_type: str = "membrane") -> float:
        """Approximate initial cargo volume from DWT."""
        if dwt_tons >= 120_000:
            return QMAX_CARGO_M3 if dwt_tons >= 150_000 else QFLEX_CARGO_M3
        return CONVENTIONAL_CARGO_M3

    def compute(
        self,
        v_initial_m3: float,
        delta_t_days: float,
        insulation_type: str = "membrane",
        reliq_type: str = "partial",
    ) -> dict[str, Any]:
        bog_nominal = BOG_RATES.get(insulation_type, 0.0015)
        reliq_eff   = RELIQ_EFFICIENCY.get(reliq_type, 0.45)
        bog_eff     = bog_nominal * (1.0 - reliq_eff)

        v_delivered  = v_initial_m3 * ((1.0 - bog_eff) ** delta_t_days)
        v_lost       = v_initial_m3 - v_delivered
        bog_loss_pct = 100.0 * v_lost / max(v_initial_m3, 1.0)

        energy_lost_mwh   = v_lost      * MWH_PER_M3_LNG
        energy_deliv_mwh  = v_delivered * MWH_PER_M3_LNG
        value_lost_eur    = energy_lost_mwh  * self.ttf_spot
        value_deliv_eur   = energy_deliv_mwh * self.ttf_spot

        return {
            "v_initial_m3":   round(v_initial_m3, 0),
            "v_delivered_m3": round(v_delivered, 0),
            "v_lost_m3":      round(v_lost, 0),
            "bog_loss_pct":   round(bog_loss_pct, 3),
            "delta_t_days":   round(delta_t_days, 1),
            "bog_effective_pct_per_day": round(bog_eff * 100, 4),
            "insulation_type": insulation_type,
            "reliq_type":      reliq_type,
            "energy_delivered_mwh": round(energy_deliv_mwh, 0),
            "energy_lost_mwh":      round(energy_lost_mwh, 0),
            "value_delivered_eur":  round(value_deliv_eur, 0),
            "value_lost_eur":       round(value_lost_eur, 0),
        }

    def fleet_bog_report(
        self,
        vessels: list[dict[str, Any]],
        now_utc: datetime | None = None,
    ) -> dict[str, Any]:
        """Aggregate BOG loss across fleet for LSSI correction."""
        now = now_utc or datetime.now(timezone.utc)
        total_init_m3    = 0.0
        total_deliv_m3   = 0.0
        total_loss_mwh   = 0.0
        total_value_eur  = 0.0
        vessel_reports   = []

        for v in vessels:
            dwt = float(v.get("dwt_tons") or 80_000)
            sog = float(v.get("sog") or 0.0)
            if sog < 0.5:
                continue   # stationary / floating storage: no active voyage

            v_init = self.estimate_cargo_m3(dwt)
            # Estimate voyage duration from SOG + lat/lon (use 10d as proxy if unavailable)
            delta_t = float(v.get("voyage_days_est") or 10.0)
            result  = self.compute(v_init, delta_t)

            total_init_m3   += v_init
            total_deliv_m3  += result["v_delivered_m3"]
            total_loss_mwh  += result["energy_lost_mwh"]
            total_value_eur += result["value_lost_eur"]
            vessel_reports.append({"imo": v.get("imo"), "name": v.get("vessel_name"), **result})

        fleet_bog_pct = 100.0 * (total_init_m3 - total_deliv_m3) / max(total_init_m3, 1.0)
        return {
            "fleet_total_init_m3":   round(total_init_m3),
            "fleet_total_deliv_m3":  round(total_deliv_m3),
            "fleet_bog_loss_pct":    round(fleet_bog_pct, 2),
            "fleet_energy_lost_mwh": round(total_loss_mwh),
            "fleet_value_lost_eur":  round(total_value_eur),
            "active_voyages":        len(vessel_reports),
            "top_loss_vessels":      sorted(vessel_reports, key=lambda x: -x["v_lost_m3"])[:5],
        }


# ─────────────────────────────────────────────────────────────────────────────
# HMM DESTINATION PREDICTOR
# ─────────────────────────────────────────────────────────────────────────────

# TTF-JKM spread threshold for Europe routing (EUR/MWh equivalent)
EUROPE_SPREAD_THRESHOLD = 2.0   # if TTF < JKM - 2 EUR/MWh → Asia pull
WAYPOINT_SUEZ    = (30.0, 32.5)   # Suez Canal approach
WAYPOINT_HORMUZ  = (26.5, 56.5)   # Strait of Hormuz
WAYPOINT_MALACCA = (1.35, 103.8)  # Malacca Strait (Asia routing)
WAYPOINT_BOSPHORUS = (41.1, 29.0) # Bosphorus (Black Sea)

# European LNG receiving terminals (anchor points)
EU_LNG_TERMINALS = [
    {"name": "Gate Rotterdam",   "lat": 51.95, "lon": 4.00,  "capacity_bcm": 12.0},
    {"name": "Isle of Grain",    "lat": 51.45, "lon": 0.73,  "capacity_bcm":  9.8},
    {"name": "Dunkerque",        "lat": 51.04, "lon": 2.21,  "capacity_bcm":  6.0},
    {"name": "Barcelona",        "lat": 41.32, "lon": 2.10,  "capacity_bcm":  9.3},
    {"name": "Revithoussa",      "lat": 37.94, "lon": 23.55, "capacity_bcm":  4.7},
    {"name": "Eemshaven",        "lat": 53.46, "lon": 6.83,  "capacity_bcm":  6.0},
    {"name": "Grain LNG",        "lat": 51.44, "lon": 0.72,  "capacity_bcm":  8.5},
]


def _bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial bearing from (lat1,lon1) → (lat2,lon2) in degrees [0, 360)."""
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δλ = math.radians(lon2 - lon1)
    y = math.sin(Δλ) * math.cos(φ2)
    x = math.cos(φ1) * math.sin(φ2) - math.sin(φ1) * math.cos(φ2) * math.cos(Δλ)
    return (math.degrees(math.atan2(y, x)) + 360) % 360


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    R = 6371.0
    φ1, φ2 = math.radians(lat1), math.radians(lat2)
    Δφ = math.radians(lat2 - lat1)
    Δλ = math.radians(lon2 - lon1)
    a = math.sin(Δφ / 2) ** 2 + math.cos(φ1) * math.cos(φ2) * math.sin(Δλ / 2) ** 2
    return 2 * R * math.asin(min(1.0, math.sqrt(a)))


class HMMDestinationPredictor:
    """
    Bayesian HMM destination predictor for LNG rerouting.

    P(Destination=Europe | spread, lat, lon, COG, SOG)

    Uses a 3-state HMM:
      State 0: Atlantic-EU-Bound    (COG ~280-360° from Red Sea / Agulhas)
      State 1: Rerouting-Uncertain  (speed anomaly or turning manoeuvre)
      State 2: Asia-Pacific-Bound   (COG ~60-120° from Hormuz / Malacca)

    Spread-weighted Bayesian update:
      P_eu_final = sigmoid(α × spread + β × bearing_eu + γ × lat_signal)
    """

    def __init__(self, ttf_spot: float = 35.0, jkm_proxy: float | None = None):
        self.ttf_spot = ttf_spot
        # JKM proxy: typically 0.85–1.15× TTF in EUR/MWh equivalent
        self.jkm_proxy = jkm_proxy if jkm_proxy is not None else ttf_spot * 0.98

    def _spread_ttf_jkm(self) -> float:
        """TTF − JKM in EUR/MWh equivalent (positive = Europe more attractive)."""
        return self.ttf_spot - self.jkm_proxy

    @staticmethod
    def _nearest_eu_terminal_bearing(lat: float, lon: float) -> float:
        """Bearing to closest EU LNG terminal as proxy for EU routing alignment."""
        min_dist = float("inf")
        best_bearing = 0.0
        for t in EU_LNG_TERMINALS:
            d = _haversine_km(lat, lon, t["lat"], t["lon"])
            if d < min_dist:
                min_dist = d
                best_bearing = _bearing_deg(lat, lon, t["lat"], t["lon"])
        return best_bearing

    @staticmethod
    def _cog_alignment_score(cog: float, target_bearing: float) -> float:
        """Cosine similarity between COG and target bearing → [0, 1]."""
        diff = abs(cog - target_bearing) % 360
        if diff > 180:
            diff = 360 - diff
        return max(0.0, 1.0 - diff / 180.0)

    def predict(self, vessel: dict[str, Any]) -> dict[str, Any]:
        lat  = float(vessel.get("lat")  or 0.0)
        lon  = float(vessel.get("lon")  or 0.0)
        cog  = float(vessel.get("cog")  or 0.0)
        sog  = float(vessel.get("sog")  or 0.0)

        # Spread signal
        spread = self._spread_ttf_jkm()
        spread_norm = math.tanh(spread / 5.0)   # normalise to ~[-1,1]

        # Geographic priors
        eu_bearing  = self._nearest_eu_terminal_bearing(lat, lon)
        cog_eu_align = self._cog_alignment_score(cog, eu_bearing)

        # Latitude signal: north of Cape of Good Hope (lat > -35) increases EU probability
        lat_signal = math.tanh((lat + 35) / 20.0)  # 0 near Cape, 1 in North Atlantic

        # In Malacca / Hormuz zone → strong Asia prior
        asia_zone_penalty = 0.0
        for wp in [WAYPOINT_MALACCA, WAYPOINT_HORMUZ]:
            d = _haversine_km(lat, lon, wp[0], wp[1])
            if d < 500:   # within 500 km
                asia_zone_penalty += (500 - d) / 500.0 * 0.4

        # Suez / Bosphorus zone → EU routing confirmed
        suez_boost = 0.0
        for wp in [WAYPOINT_SUEZ, WAYPOINT_BOSPHORUS]:
            d = _haversine_km(lat, lon, wp[0], wp[1])
            if d < 300:
                suez_boost += (300 - d) / 300.0 * 0.5

        # Logistic regression score
        α, β, γ = 0.25, 0.40, 0.20
        score = α * spread_norm + β * cog_eu_align + γ * lat_signal
        score += suez_boost - asia_zone_penalty
        p_eu = 1.0 / (1.0 + math.exp(-3.0 * score))  # sigmoid

        # HMM state
        if sog < 1.0:
            state = "rerouting_uncertain"
            p_eu = max(p_eu - 0.1, 0.0)  # uncertainty penalty for stopped vessels
        elif p_eu > 0.65:
            state = "eu_bound"
        elif p_eu < 0.35:
            state = "asia_bound"
        else:
            state = "rerouting_uncertain"

        return {
            "p_europe":     round(p_eu, 3),
            "p_asia":       round(1.0 - p_eu, 3),
            "hmm_state":    state,
            "spread_signal": round(spread, 2),
            "eu_bearing":   round(eu_bearing, 1),
            "cog_eu_align": round(cog_eu_align, 3),
            "asia_zone_penalty": round(asia_zone_penalty, 3),
            "suez_boost":   round(suez_boost, 3),
        }

    def fleet_routing_report(
        self, vessels: list[dict[str, Any]]
    ) -> dict[str, Any]:
        eu_bound, asia_bound, uncertain = [], [], []
        p_eu_sum = 0.0
        for v in vessels:
            pred = self.predict(v)
            v_out = {
                "imo": v.get("imo"), "name": v.get("vessel_name"),
                "tier": v.get("tier"), **pred
            }
            p_eu_sum += pred["p_europe"]
            if pred["hmm_state"] == "eu_bound":
                eu_bound.append(v_out)
            elif pred["hmm_state"] == "asia_bound":
                asia_bound.append(v_out)
            else:
                uncertain.append(v_out)

        n = max(len(vessels), 1)
        return {
            "eu_bound_count":   len(eu_bound),
            "asia_bound_count": len(asia_bound),
            "uncertain_count":  len(uncertain),
            "fleet_p_eu_avg":   round(p_eu_sum / n, 3),
            "spread_ttf_jkm":   round(self._spread_ttf_jkm(), 2),
            "top_eu_bound":     sorted(eu_bound,   key=lambda x: -x["p_europe"])[:8],
            "top_asia_bound":   sorted(asia_bound, key=lambda x:  x["p_europe"])[:8],
            "rerouting_risk":   sorted(uncertain,  key=lambda x: -abs(x["p_europe"] - 0.5))[:8],
        }


# ─────────────────────────────────────────────────────────────────────────────
# NOAA HYDRODYNAMICS ETA ENGINE
# ─────────────────────────────────────────────────────────────────────────────

# Major ocean current vectors (lat_center, lon_center, speed_kn, direction_deg, width_km)
# Source: NOAA OSCAR / Copernicus Global Ocean Analysis
OCEAN_CURRENTS = [
    {"name": "Gulf Stream",   "lat": 38.0,  "lon": -65.0,  "speed_kn": 2.5, "dir": 45,  "width_km": 150},
    {"name": "Agulhas",       "lat": -35.0, "lon": 27.0,   "speed_kn": 3.0, "dir": 235, "width_km": 100},
    {"name": "Agulhas Return","lat": -40.0, "lon": 40.0,   "speed_kn": 1.5, "dir": 90,  "width_km": 200},
    {"name": "Kuroshio",      "lat": 33.0,  "lon": 140.0,  "speed_kn": 2.0, "dir": 45,  "width_km": 120},
    {"name": "Equatorial_N",  "lat": 10.0,  "lon": 50.0,   "speed_kn": 0.8, "dir": 270, "width_km": 300},
    {"name": "Equatorial_S",  "lat": -5.0,  "lon": 70.0,   "speed_kn": 0.6, "dir": 260, "width_km": 350},
    {"name": "Somali Current","lat": 10.0,  "lon": 50.0,   "speed_kn": 1.5, "dir": 45,  "width_km": 100},
    {"name": "Leeuwin",       "lat": -30.0, "lon": 115.0,  "speed_kn": 0.7, "dir": 180, "width_km": 150},
    {"name": "Canary",        "lat": 28.0,  "lon": -18.0,  "speed_kn": 0.5, "dir": 200, "width_km": 300},
]

# Beaufort scale wave resistance factor (added resistance coefficient per kn SOG)
# Based on: ΔP_wave ≈ 0.5 × ρ × Cw × H_s² × B (simplified)
BEAUFORT_RESISTANCE = {
    0: 0.00, 1: 0.00, 2: 0.01, 3: 0.02, 4: 0.04,
    5: 0.08, 6: 0.15, 7: 0.25, 8: 0.38, 9: 0.55,
    10: 0.75, 11: 1.00, 12: 1.50,
}

# North Atlantic typical Beaufort (monthly seasonal average, simplified)
SEASONAL_BEAUFORT = {1: 6, 2: 6, 3: 5, 4: 4, 5: 3, 6: 3, 7: 3, 8: 4, 9: 5, 10: 6, 11: 6, 12: 6}


def _current_effect_kn(lat: float, lon: float, cog: float) -> float:
    """
    Net head/tail current component in knots for a vessel at (lat, lon) heading COG.
    Positive = favourable (adds to speed), negative = adverse.
    """
    total_effect = 0.0
    for c in OCEAN_CURRENTS:
        dist = _haversine_km(lat, lon, c["lat"], c["lon"])
        if dist > c["width_km"]:
            continue
        # Gaussian decay within current width
        weight = math.exp(-0.5 * (dist / (c["width_km"] / 2.5)) ** 2)
        current_dir = c["dir"]
        current_spd = c["speed_kn"]
        # Project current onto vessel COG direction
        angle_diff_rad = math.radians(current_dir - cog)
        projection = current_spd * math.cos(angle_diff_rad)   # +: tailwind, -: headwind
        total_effect += weight * projection
    return round(total_effect, 2)


class HydrodynamicsETAEngine:
    """
    True ETA calculator incorporating:
      - Ocean current vectors (OSCAR/Copernicus)
      - Wave added resistance (Beaufort scale)
      - Seasonal weather correction factors

    True_SOG = Reported_SOG + Current_effect - Wave_resistance_penalty
    ETA_correction_hours = (Route_km / True_SOG_kn × 0.5396) − (Route_km / Nominal_SOG_kn × 0.5396)
    """

    @staticmethod
    def _wave_resistance_kn(beaufort: int, sog: float) -> float:
        coeff = BEAUFORT_RESISTANCE.get(min(beaufort, 12), 0.0)
        return coeff * (sog / 15.0)   # normalised to 15-kn nominal

    def compute_eta(
        self,
        lat: float, lon: float,
        cog: float, sog: float,
        dest_lat: float, dest_lon: float,
        month: int = 6,
    ) -> dict[str, Any]:
        if sog < 0.3:
            return {"eta_days": None, "eta_correction_hours": 0.0, "current_effect_kn": 0.0}

        dist_km   = _haversine_km(lat, lon, dest_lat, dest_lon)
        dist_nm   = dist_km * 0.5396   # km → nautical miles

        bft       = SEASONAL_BEAUFORT.get(month, 4)
        wave_pen  = self._wave_resistance_kn(bft, sog)
        cur_eff   = _current_effect_kn(lat, lon, cog)

        true_sog   = max(0.5, sog + cur_eff - wave_pen)
        nominal_eta_h = dist_nm / sog
        true_eta_h    = dist_nm / true_sog
        correction_h  = true_eta_h - nominal_eta_h

        return {
            "route_nm":             round(dist_nm, 0),
            "reported_sog_kn":      round(sog, 1),
            "current_effect_kn":    round(cur_eff, 2),
            "wave_resistance_kn":   round(wave_pen, 2),
            "true_sog_kn":          round(true_sog, 1),
            "nominal_eta_h":        round(nominal_eta_h, 1),
            "true_eta_h":           round(true_eta_h, 1),
            "eta_correction_hours": round(correction_h, 1),
            "beaufort":             bft,
        }

    def fleet_eta_report(
        self,
        vessels: list[dict[str, Any]],
        month: int | None = None,
    ) -> dict[str, Any]:
        month = month or datetime.now(timezone.utc).month
        reports = []
        total_correction_h = 0.0
        delayed = 0

        for v in vessels:
            sog = float(v.get("sog") or 0.0)
            if sog < 0.3:
                continue
            lat = float(v.get("lat") or 0.0)
            lon = float(v.get("lon") or 0.0)
            cog = float(v.get("cog") or 0.0)
            # Approximate EU delivery: nearest EU terminal
            best_t = min(EU_LNG_TERMINALS, key=lambda t: _haversine_km(lat, lon, t["lat"], t["lon"]))
            result = self.compute_eta(lat, lon, cog, sog, best_t["lat"], best_t["lon"], month)

            total_correction_h += result["eta_correction_hours"]
            if result["eta_correction_hours"] > 12:
                delayed += 1

            reports.append({
                "imo": v.get("imo"),
                "name": v.get("vessel_name"),
                "dest_terminal": best_t["name"],
                **result,
            })

        n = max(len(reports), 1)
        return {
            "vessels_analysed":       len(reports),
            "avg_eta_correction_h":   round(total_correction_h / n, 1),
            "delayed_more_than_12h":  delayed,
            "top_delayed":            sorted(reports, key=lambda x: -x["eta_correction_hours"])[:8],
            "month":                  month,
        }


# ─────────────────────────────────────────────────────────────────────────────
# TERMINAL BOTTLENECK INDEX (Regasification Queue)
# ─────────────────────────────────────────────────────────────────────────────

# EU LNG terminal approach zones (lat, lon, radius_nm)
TERMINAL_ZONES = [
    {"name": "Gate Rotterdam",  "lat": 51.95, "lon": 4.00,  "radius_nm": 25, "capacity_ships_pw": 4},
    {"name": "Isle of Grain",   "lat": 51.45, "lon": 0.73,  "radius_nm": 20, "capacity_ships_pw": 3},
    {"name": "Dunkerque",       "lat": 51.04, "lon": 2.21,  "radius_nm": 20, "capacity_ships_pw": 3},
    {"name": "Barcelona",       "lat": 41.32, "lon": 2.10,  "radius_nm": 18, "capacity_ships_pw": 3},
    {"name": "Revithoussa",     "lat": 37.94, "lon": 23.55, "radius_nm": 15, "capacity_ships_pw": 2},
    {"name": "Eemshaven",       "lat": 53.46, "lon": 6.83,  "radius_nm": 20, "capacity_ships_pw": 2},
]

# Berth throughput: typically 36h per Q-Max unloading + 12h transit
BERTH_HOURS_PER_VESSEL = 48.0


class TerminalBottleneckIndex:
    """
    Spatial clustering of LNG vessels around EU regasification terminals.

    TBI = Σ_terminals (vessels_drifting_in_zone / capacity_per_week)

    Vessels drifting: SOG < 0.5 kn within terminal radius.
    Injection delay: (queue_depth − 1) × BERTH_HOURS_PER_VESSEL
    """

    @staticmethod
    def _vessels_in_zone(
        vessels: list[dict[str, Any]],
        center_lat: float, center_lon: float,
        radius_nm: float,
    ) -> list[dict[str, Any]]:
        R_NM = 3440.065
        result = []
        for v in vessels:
            lat = float(v.get("lat") or 0.0)
            lon = float(v.get("lon") or 0.0)
            dist_km = _haversine_km(lat, lon, center_lat, center_lon)
            dist_nm = dist_km * 0.5396
            if dist_nm <= radius_nm:
                result.append({**v, "_dist_nm": round(dist_nm, 1)})
        return result

    def compute(self, vessels: list[dict[str, Any]]) -> dict[str, Any]:
        terminal_reports = []
        total_tbi = 0.0
        total_delayed_mwh = 0.0

        for zone in TERMINAL_ZONES:
            vessels_in = self._vessels_in_zone(
                vessels, zone["lat"], zone["lon"], zone["radius_nm"]
            )
            drifting    = [v for v in vessels_in if float(v.get("sog") or 0) < 0.5]
            slow_approach = [v for v in vessels_in if 0.5 <= float(v.get("sog") or 0) < 2.0]

            queue_depth = len(drifting)
            cap         = zone["capacity_ships_pw"]
            congestion_ratio = queue_depth / max(cap, 1)
            inject_delay_h   = max(0, queue_depth - 1) * BERTH_HOURS_PER_VESSEL

            # Delayed MWh: each drifting vessel carries ~216,000 m³ × 2.637 MWh/m³
            delayed_mwh = queue_depth * 216_000 * MWH_PER_M3_LNG
            total_delayed_mwh += delayed_mwh
            total_tbi += congestion_ratio

            terminal_reports.append({
                "terminal":          zone["name"],
                "queue_depth":       queue_depth,
                "slow_approach":     len(slow_approach),
                "capacity_per_week": cap,
                "congestion_ratio":  round(congestion_ratio, 2),
                "inject_delay_h":    round(inject_delay_h, 0),
                "delayed_mwh":       round(delayed_mwh, 0),
                "drifting_vessels":  [
                    {"imo": v.get("imo"), "name": v.get("vessel_name"), "dist_nm": v["_dist_nm"]}
                    for v in drifting[:5]
                ],
            })

        avg_tbi = round(total_tbi / max(len(TERMINAL_ZONES), 1), 3)
        return {
            "tbi_score":          avg_tbi,
            "total_delayed_mwh":  round(total_delayed_mwh, 0),
            "injection_delay_signal": "HIGH" if avg_tbi > 0.6 else "MEDIUM" if avg_tbi > 0.3 else "LOW",
            "terminals":          terminal_reports,
        }


# ─────────────────────────────────────────────────────────────────────────────
# ICE MARKET MICROSTRUCTURE ENSEMBLE
# ─────────────────────────────────────────────────────────────────────────────

class ICEMicrostructureEnsemble:
    """
    Hybrid Physical Flow × ICE Orderbook Microstructure Ensemble.

    Signal construction:
      1. AIS flow signal: net LNG m³ moving toward EU (from HMM routing)
      2. COT net position proxy: derived from Markov regime + CatBoost P50 trend
      3. Orderbook imbalance proxy: inferred from TTF/JKM spread momentum
      4. Combined signal → directional probability + confidence interval

    Directional accuracy metric:
        Acc = (TP + TN) / (TP + TN + FP + FN)
        Target: >80% on 5-day rolling window.
    """

    def __init__(self, ttf_forecast: dict[str, Any], bog_report: dict[str, Any]):
        self.ttf = ttf_forecast
        self.bog = bog_report

    def _flow_signal(self, routing: dict[str, Any]) -> float:
        """EU-bound LNG flow as fraction of fleet total → [0, 1]."""
        eu_count   = routing.get("eu_bound_count", 0)
        total      = max(routing.get("eu_bound_count", 0) + routing.get("asia_bound_count", 0) + routing.get("uncertain_count", 0), 1)
        return eu_count / total

    def _cot_proxy(self, markov_state: str) -> float:
        """
        CFTC/COT net position proxy from Markov state:
          LowVol_Accumulation → mild long bias  (+0.2)
          MeanReverting_Transit → neutral       ( 0.0)
          HighVol_SupplyShock → strong long     (+0.5)
        """
        state_map = {
            "LowVol_Accumulation":   0.20,
            "MeanReverting_Transit": 0.00,
            "HighVol_SupplyShock":   0.50,
        }
        # Handle partial key match
        for k, v in state_map.items():
            if k.lower() in str(markov_state).lower():
                return v
        return 0.10  # mild bullish default

    def _orderbook_imbalance(self, spread: float) -> float:
        """
        TTF-JKM spread momentum as orderbook imbalance proxy.
        Positive spread → EU demand pull → buy imbalance → +signal.
        """
        return math.tanh(spread / 8.0)   # normalised sigmoid

    def compute(
        self,
        routing: dict[str, Any],
        markov_state: str = "MeanReverting_Transit",
        tbi: dict[str, Any] | None = None,
        catboost_p50: float | None = None,
    ) -> dict[str, Any]:
        spot   = float(self.ttf.get("spot_eur_mwh") or 35.0)
        h7_p50 = catboost_p50 or float((self.ttf.get("kpi") or {}).get("h7_p50") or spot)
        spread = routing.get("spread_ttf_jkm", 0.0)

        # Feature vector
        f_flow     = self._flow_signal(routing)
        f_cot      = self._cot_proxy(markov_state)
        f_ob       = self._orderbook_imbalance(spread)
        f_bog      = min(1.0, float(self.bog.get("fleet_bog_loss_pct", 0)) / 2.0)  # normalised
        f_tbi      = float((tbi or {}).get("tbi_score", 0.0))
        f_momentum = math.tanh((h7_p50 - spot) / max(spot, 1.0) * 20)

        # Ensemble weights (inspired by ensemble WEIGHTS in ensemble_aggregator.py)
        w_flow, w_cot, w_ob, w_bog, w_tbi, w_mom = 0.30, 0.20, 0.20, 0.10, 0.10, 0.10
        raw_signal = (
            w_flow * f_flow
            + w_cot * f_cot
            + w_ob  * f_ob
            - w_bog * f_bog      # BOG loss = supply reduction → bullish for price
            + w_tbi * f_tbi      # Terminal congestion → delayed injection → bullish
            + w_mom * f_momentum
        )

        p_bull = 1.0 / (1.0 + math.exp(-5.0 * raw_signal))   # logistic
        p_bear = 1.0 - p_bull
        confidence = abs(p_bull - 0.5) * 2.0   # 0=pure uncertainty, 1=full confidence

        # Directional call
        if p_bull > 0.60:
            direction = "LONG"
        elif p_bull < 0.40:
            direction = "SHORT"
        else:
            direction = "NEUTRAL"

        # Price target: CatBoost P50 adjusted by ensemble signal
        adjustment = (raw_signal - 0.5) * spot * 0.04   # ±4% max adjustment
        target_h7  = round(h7_p50 + adjustment, 2)

        # Accuracy proxy: calibrate against Brier score on Markov regime priors
        brier_proxy = round(1.0 - (confidence - 0.3) ** 2, 3)   # heuristic calibration

        return {
            "direction":      direction,
            "p_bull":         round(p_bull, 3),
            "p_bear":         round(p_bear, 3),
            "confidence":     round(confidence, 3),
            "raw_signal":     round(raw_signal, 4),
            "target_h7":      target_h7,
            "features": {
                "f_flow":      round(f_flow, 3),
                "f_cot_proxy": round(f_cot, 3),
                "f_orderbook": round(f_ob, 3),
                "f_bog":       round(f_bog, 3),
                "f_tbi":       round(f_tbi, 3),
                "f_momentum":  round(f_momentum, 3),
            },
            "accuracy_proxy": brier_proxy,
            "markov_input":   markov_state,
        }


# ─────────────────────────────────────────────────────────────────────────────
# MASTER QUANT PIPELINE
# ─────────────────────────────────────────────────────────────────────────────

def _load_markov_state(out_path: Path) -> str:
    """Load current Markov state from output artifacts."""
    for fname in ("ttf_markov_regimes.json", "features/markov_transition_matrix.json",
                  "features/spectral_report.json"):
        p = out_path / fname
        if not p.exists():
            # Try root/output
            p = out_path.parent / "output" / fname
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                state = d.get("current_state_name") or d.get("state") or ""
                if state:
                    return str(state)
            except Exception:  # noqa: BLE001
                pass
    return "MeanReverting_Transit"  # safe default


def _load_ttf_forecast(out_path: Path) -> dict[str, Any]:
    """Load TTF ensemble forecast from disk."""
    p = out_path / "ttf_ensemble_forecast.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            pass
    return {}


def build_quant_pipeline_payload(
    sentinel_payload: dict[str, Any],
    *,
    run_full: bool = True,
) -> dict[str, Any]:
    """
    Master quant pipeline: runs all 5 mathematical models against current fleet snapshot.
    Integrated with existing sentinel_payload from build_sentinel_payload().

    Returns a dict merged into payload["quant_pipeline"].
    """
    start = time.monotonic()
    vessels = sentinel_payload.get("c01_heatmap") or []
    ttf_meta = sentinel_payload.get("ttf_forecast") or {}
    sts_clusters = sentinel_payload.get("c03_sts_clusters") or []

    ttf_spot = float(ttf_meta.get("spot_eur_mwh") or 35.0)
    h7_p50   = float((ttf_meta.get("kpi") or {}).get("h7_p50") or ttf_spot)

    markov_state = str(
        (ttf_meta.get("markov_regime") or {}).get("current_state_name")
        or _load_markov_state(ROOT / "output")
    )

    errors: list[str] = []

    # ── 1. BOG Decay Engine ──────────────────────────────────────────────────
    bog_report: dict[str, Any] = {}
    try:
        bog_eng = BOGDecayEngine(ttf_spot=ttf_spot)
        bog_report = bog_eng.fleet_bog_report(vessels)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"BOG: {exc}")
        bog_report = {"error": str(exc)}

    # ── 2. HMM Destination Predictor ─────────────────────────────────────────
    routing_report: dict[str, Any] = {}
    try:
        jkm_proxy = ttf_spot * 0.97   # synthetic JKM proxy (no real-time feed)
        hmm = HMMDestinationPredictor(ttf_spot=ttf_spot, jkm_proxy=jkm_proxy)
        routing_report = hmm.fleet_routing_report(vessels)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"HMM: {exc}")
        routing_report = {"error": str(exc)}

    # ── 3. NOAA Hydrodynamics ETA ────────────────────────────────────────────
    eta_report: dict[str, Any] = {}
    try:
        hydro = HydrodynamicsETAEngine()
        eta_report = hydro.fleet_eta_report(vessels)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"NOAA: {exc}")
        eta_report = {"error": str(exc)}

    # ── 4. Terminal Bottleneck Index ─────────────────────────────────────────
    tbi_report: dict[str, Any] = {}
    try:
        tbi = TerminalBottleneckIndex()
        tbi_report = tbi.compute(vessels)
    except Exception as exc:  # noqa: BLE001
        errors.append(f"TBI: {exc}")
        tbi_report = {"error": str(exc)}

    # ── 5. ICE Microstructure Ensemble ───────────────────────────────────────
    ice_report: dict[str, Any] = {}
    try:
        ice = ICEMicrostructureEnsemble(ttf_forecast=ttf_meta, bog_report=bog_report)
        ice_report = ice.compute(
            routing=routing_report,
            markov_state=markov_state,
            tbi=tbi_report,
            catboost_p50=h7_p50,
        )
    except Exception as exc:  # noqa: BLE001
        errors.append(f"ICE: {exc}")
        ice_report = {"error": str(exc)}

    # ── Dual-Truth elimination ────────────────────────────────────────────────
    # (a) purged_cv_directional — weighted mean of component directional accuracies
    # (b) confidence_proxy — ICE |p_bull−0.5|×2×100 (diagnostic only)
    # Production KPI = (a). Never publish (b) as unlabeled ensemble_accuracy_pct.
    from services.ttf_forecast.ensemble_aggregator import (
        adaptive_ensemble_weights,
        load_component_directional_accuracy,
    )

    component_acc = load_component_directional_accuracy()
    eff_weights, weight_governance = adaptive_ensemble_weights()
    catboost_acc = float(component_acc["catboost"]) if component_acc["catboost"] is not None else 0.0
    ensemble_confidence_proxy_pct = round(
        float(ice_report.get("confidence") or 0.0) * 100.0, 1
    )

    weighted_sum = 0.0
    weight_tot = 0.0
    for name, acc in component_acc.items():
        if acc is None:
            continue
        w_i = float(eff_weights.get(name) or 0.0)
        if w_i <= 0:
            continue
        weighted_sum += float(acc) * w_i
        weight_tot += w_i

    if weight_tot > 0:
        ensemble_accuracy_pct = round(weighted_sum / weight_tot, 1)
        accuracy_basis = "purged_cv_directional"
    else:
        ensemble_accuracy_pct = ensemble_confidence_proxy_pct
        accuracy_basis = "confidence_proxy"
        catboost_acc = 0.0

    # Dual-gate: never let offline CV silently stand in for live fleet confidence
    from services.dual_gate import compute_fleet_sample_status, live_inference_confidence

    cov_n = int(
        sentinel_payload.get("top500_live_coverage")
        or (sentinel_payload.get("top500_coverage") or {}).get("top500_live_coverage")
        or 0
    )
    if not cov_n:
        try:
            from services.ais_health import compute_top500_live_coverage

            cov_n = int(compute_top500_live_coverage().get("top500_live_coverage") or 0)
        except Exception:  # noqa: BLE001
            cov_n = len(vessels)
    sample = compute_fleet_sample_status(cov_n)
    live_conf = live_inference_confidence(
        model_cv_accuracy_pct=ensemble_accuracy_pct if accuracy_basis == "purged_cv_directional" else None,
        fleet_sample_status=sample["fleet_sample_status"],
        coverage=cov_n,
    )

    elapsed_ms = round((time.monotonic() - start) * 1000, 1)

    return {
        "generated_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "elapsed_ms":        elapsed_ms,
        "fleet_size":        len(vessels),
        "ttf_spot":          ttf_spot,
        "markov_state":      markov_state,
        "errors":            errors,
        "bog":     bog_report,
        "routing": routing_report,
        "eta":     eta_report,
        "tbi":     tbi_report,
        "ice":     ice_report,
        # (i) offline model property — valid regardless of live coverage
        "ensemble_accuracy_pct": ensemble_accuracy_pct,
        "model_cv_accuracy_pct": live_conf.get("model_cv_accuracy_pct"),
        "accuracy_basis": accuracy_basis,  # purged_cv_directional | confidence_proxy
        "ensemble_confidence_proxy_pct": ensemble_confidence_proxy_pct,
        "catboost_accuracy_pct": round(catboost_acc, 1),
        "component_accuracy_pct": {
            k: (round(v, 1) if v is not None else None) for k, v in component_acc.items()
        },
        "ensemble_weights": {k: round(float(v), 4) for k, v in eff_weights.items()},
        "weight_governance": weight_governance,
        "direction":             ice_report.get("direction", "NEUTRAL"),
        "confidence":            ice_report.get("confidence", 0.0),
        # (ii) live representativeness — MUST drop when fleet_sample != FULL
        "fleet_sample_status": sample["fleet_sample_status"],
        "sample_size_caveat": sample.get("sample_size_caveat"),
        "live_inference_confidence": live_conf.get("live_inference_confidence"),
        "live_inference_confidence_pct": live_conf.get("live_inference_confidence_pct"),
        "live_confidence_factor": live_conf.get("live_confidence_factor"),
        "live_confidence_note": live_conf.get("note"),
        "top500_live_coverage": cov_n,
    }

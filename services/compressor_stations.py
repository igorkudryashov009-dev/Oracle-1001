"""Compressor stations GIS SoT — Contract 1.8.0-ops-gis-sot.

Canonical registry: 185 stations. Point geometry is the bbox centroid of the
validated geofence catalogs. ``capacity_bcm_y`` is a corridor-class notional
estimate (not live SCADA). Bbox format: [lon_min, lat_min, lon_max, lat_max].
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

CONTRACT_VERSION = "1.8.0-ops-gis-sot"


@dataclass(frozen=True)
class CompressorStation:
    id: str
    name: str
    lat: float
    lon: float
    capacity_bcm_y: float
    corridor: str
    operator: str
    legacy_id: str = ""
    bbox: tuple[float, float, float, float] | None = None


def _cs(
    id: str,
    name: str,
    lat: float,
    lon: float,
    capacity_bcm_y: float,
    corridor: str,
    operator: str,
    legacy_id: str,
    bbox: list[float],
) -> CompressorStation:
    return CompressorStation(
        id=id,
        name=name,
        lat=lat,
        lon=lon,
        capacity_bcm_y=capacity_bcm_y,
        corridor=corridor,
        operator=operator,
        legacy_id=legacy_id,
        bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
    )


ALL_COMPRESSOR_STATIONS: tuple[CompressorStation, ...] = (
    _cs('CS-001', 'Atamanskaya Blagoveshchensk', 50.425, 127.54, 38.5, 'Power_of_Siberia', 'Gazprom', 'KS_01_Atamanskaya_Blagoveshchensk', [127.52, 50.41, 127.56, 50.44]),
    _cs('CS-002', 'Saldykelskaya IvanRebrov', 59.915, 119.84, 39.0, 'Power_of_Siberia', 'Gazprom', 'KS_02_Saldykelskaya_IvanRebrov', [119.82, 59.9, 119.86, 59.93]),
    _cs('CS-003', 'Olyokminskaya PyotrBeketov', 60.335, 120.42, 39.5, 'Power_of_Siberia', 'Gazprom', 'KS_03_Olyokminskaya_PyotrBeketov', [120.4, 60.32, 120.44, 60.35]),
    _cs('CS-004', 'Amginskaya MaximPerfilyev', 59.095, 124.97, 40.0, 'Power_of_Siberia', 'Gazprom', 'KS_04_Amginskaya_MaximPerfilyev', [124.95, 59.08, 124.99, 59.11]),
    _cs('CS-005', 'Nimnyrskaya IvanMoskvitin', 57.315, 125.77, 40.5, 'Power_of_Siberia', 'Gazprom', 'KS_05_Nimnyrskaya_IvanMoskvitin', [125.75, 57.3, 125.79, 57.33]),
    _cs('CS-006', 'Nagornaya VasilyPoyarkov', 55.935, 124.92, 41.0, 'Power_of_Siberia', 'Gazprom', 'KS_06_Nagornaya_VasilyPoyarkov', [124.9, 55.92, 124.94, 55.95]),
    _cs('CS-007', 'Skovorodinskaya ErofeyKhabarov', 53.995, 123.97, 38.0, 'Power_of_Siberia', 'Gazprom', 'KS_07_Skovorodinskaya_ErofeyKhabarov', [123.95, 53.98, 123.99, 54.01]),
    _cs('CS-008', 'Zeyskaya VasilyKolesnikov', 53.735, 127.07, 38.5, 'Power_of_Siberia', 'Gazprom', 'KS_08_Zeyskaya_VasilyKolesnikov', [127.05, 53.72, 127.09, 53.75]),
    _cs('CS-009', 'Slavyanskaya NordStream', 59.695, 28.07, 56.0, 'Nord_Stream_Northern', 'Gazprom', 'KS_09_Slavyanskaya_NordStream', [28.05, 59.68, 28.09, 59.71]),
    _cs('CS-010', 'Portovaya NordStream1', 60.565, 28.05, 56.5, 'Nord_Stream_Northern', 'Gazprom', 'KS_10_Portovaya_NordStream1', [28.03, 60.55, 28.07, 60.58]),
    _cs('CS-011', 'Baidaratskaya Yamal', 68.865, 68.34, 57.0, 'Nord_Stream_Northern', 'Gazprom', 'KS_11_Baidaratskaya_Yamal', [68.32, 68.85, 68.36, 68.88]),
    _cs('CS-012', 'Gagaratckaya Ukhta', 67.565, 65.92, 57.5, 'Nord_Stream_Northern', 'Gazprom', 'KS_12_Gagaratckaya_Ukhta', [65.9, 67.55, 65.94, 67.58]),
    _cs('CS-013', 'Yarynskaya Ukhta', 66.435, 64.22, 58.0, 'Nord_Stream_Northern', 'Gazprom', 'KS_13_Yarynskaya_Ukhta', [64.2, 66.42, 64.24, 66.45]),
    _cs('CS-014', 'Ukhtinskaya', 63.595, 53.52, 55.0, 'Nord_Stream_Northern', 'Gazprom', 'KS_14_Ukhtinskaya', [53.5, 63.58, 53.54, 63.61]),
    _cs('CS-015', 'Gryazovetskaya Hub', 58.865, 40.52, 55.5, 'Nord_Stream_Northern', 'Gazprom', 'KS_15_Gryazovetskaya_Hub', [40.5, 58.85, 40.54, 58.88]),
    _cs('CS-016', 'Pikalevskaya', 59.515, 35.17, 56.0, 'Nord_Stream_Northern', 'Gazprom', 'KS_16_Pikalevskaya', [35.15, 59.5, 35.19, 59.53]),
    _cs('CS-017', 'Volchov SPb', 59.925, 32.32, 56.5, 'Nord_Stream_Northern', 'Gazprom', 'KS_17_Volchov_SPb', [32.3, 59.91, 32.34, 59.94]),
    _cs('CS-018', 'Vyborskaya', 60.715, 28.77, 57.0, 'Nord_Stream_Northern', 'Gazprom', 'KS_18_Vyborskaya', [28.75, 60.7, 28.79, 60.73]),
    _cs('CS-019', 'Russkaya TurkStream', 44.835, 37.8, 34.0, 'TurkStream_South', 'Gazprom', 'KS_19_Russkaya_TurkStream', [37.78, 44.82, 37.82, 44.85]),
    _cs('CS-020', 'Korenovskaya TurkStream', 45.465, 39.44, 34.5, 'TurkStream_South', 'Gazprom', 'KS_20_Korenovskaya_TurkStream', [39.42, 45.45, 39.46, 45.48]),
    _cs('CS-021', 'Shakhtinskaya South', 47.695, 40.22, 31.5, 'TurkStream_South', 'Gazprom', 'KS_21_Shakhtinskaya_South', [40.2, 47.68, 40.24, 47.71]),
    _cs('CS-022', 'Pisarevkaya Voronezh', 49.835, 40.07, 32.0, 'TurkStream_South', 'Gazprom', 'KS_22_Pisarevkaya_Voronezh', [40.05, 49.82, 40.09, 49.85]),
    _cs('CS-023', 'Sudzha Border Transit', 51.185, 35.2, 32.5, 'TurkStream_South', 'Gazprom', 'KS_23_Sudzha_Border_Transit', [35.18, 51.17, 35.22, 51.2]),
    _cs('CS-024', 'Sohranovka Border', 49.265, 39.82, 33.0, 'TurkStream_South', 'Gazprom', 'KS_24_Sohranovka_Border', [39.8, 49.25, 39.84, 49.28]),
    _cs('CS-025', 'Mozdok Caucasus', 43.735, 44.64, 33.5, 'TurkStream_South', 'Gazprom', 'KS_25_Mozdok_Caucasus', [44.62, 43.72, 44.66, 43.75]),
    _cs('CS-026', 'Izobilny South Hub', 45.365, 41.72, 34.0, 'TurkStream_South', 'Gazprom', 'KS_26_Izobilny_South_Hub', [41.7, 45.35, 41.74, 45.38]),
    _cs('CS-027', 'Urengoyskaya Hub', 66.095, 76.64, 43.0, 'Urengoy_Central', 'Gazprom', 'KS_27_Urengoyskaya_Hub', [76.62, 66.08, 76.66, 66.11]),
    _cs('CS-028', 'Pangodinskaya', 65.835, 74.47, 40.0, 'Urengoy_Central', 'Gazprom', 'KS_28_Pangodinskaya', [74.45, 65.82, 74.49, 65.85]),
    _cs('CS-029', 'Nadbmskaya', 65.535, 72.52, 40.5, 'Urengoy_Central', 'Gazprom', 'KS_29_Nadbmskaya', [72.5, 65.52, 72.54, 65.55]),
    _cs('CS-030', 'Punginskaya Storage', 62.365, 64.5, 41.0, 'Urengoy_Central', 'Gazprom', 'KS_30_Punginskaya_Storage', [64.48, 62.35, 64.52, 62.38]),
    _cs('CS-031', 'Taezhnaya Yugra', 61.235, 62.92, 41.5, 'Urengoy_Central', 'Gazprom', 'KS_31_Taezhnaya_Yugra', [62.9, 61.22, 62.94, 61.25]),
    _cs('CS-032', 'Komsomolskaya', 62.095, 66.04, 42.0, 'Urengoy_Central', 'Gazprom', 'KS_32_Komsomolskaya', [66.02, 62.08, 66.06, 62.11]),
    _cs('CS-033', 'Permskaya Ural', 58.015, 56.22, 42.5, 'Urengoy_Central', 'Gazprom', 'KS_33_Permskaya_Ural', [56.2, 58.0, 56.24, 58.03]),
    _cs('CS-034', 'Malmyzhskaya', 56.635, 50.64, 43.0, 'Urengoy_Central', 'Gazprom', 'KS_34_Malmyzhskaya', [50.62, 56.62, 50.66, 56.65]),
    _cs('CS-035', 'Pomarskaya MariEl', 55.895, 48.34, 28.0, 'Progress_Pomary', 'Gazprom', 'KS_35_Pomarskaya_MariEl', [48.32, 55.88, 48.36, 55.91]),
    _cs('CS-036', 'Pochinki NizhnyNovgorod', 54.695, 44.84, 28.5, 'Progress_Pomary', 'Gazprom', 'KS_36_Pochinki_NizhnyNovgorod', [44.82, 54.68, 44.86, 54.71]),
    _cs('CS-037', 'Torzhok Western Hub', 57.035, 34.94, 29.0, 'Progress_Pomary', 'Gazprom', 'KS_37_Torzhok_Western_Hub', [34.92, 57.02, 34.96, 57.05]),
    _cs('CS-038', 'Petrovsk Saratov', 52.315, 45.42, 29.5, 'Progress_Pomary', 'Gazprom', 'KS_38_Petrovsk_Saratov', [45.4, 52.3, 45.44, 52.33]),
    _cs('CS-039', 'Orenburgskaya', 51.735, 55.1, 30.0, 'Progress_Pomary', 'Gazprom', 'KS_39_Orenburgskaya', [55.08, 51.72, 55.12, 51.75]),
    _cs('CS-040', 'Mokrousovskaya', 51.235, 47.52, 30.5, 'Progress_Pomary', 'Gazprom', 'KS_40_Mokrousovskaya', [47.5, 51.22, 47.54, 51.25]),
    _cs('CS-041', 'Volgogradskaya', 48.795, 44.5, 31.0, 'Progress_Pomary', 'Gazprom', 'KS_41_Volgogradskaya', [44.48, 48.78, 44.52, 48.81]),
    _cs('CS-042', 'Yelets Lipetsk Hub', 52.615, 38.54, 28.0, 'Progress_Pomary', 'Gazprom', 'KS_42_Yelets_Lipetsk_Hub', [38.52, 52.6, 38.56, 52.63]),
    _cs('CS-043', 'Saldykel Yakutia', 60.115, 119.82, 20.5, 'Far_East_Transit', 'Gazprom', 'KS_43_Saldykel_Yakutia', [119.8, 60.1, 119.84, 60.13]),
    _cs('CS-044', 'Sakhalin Booster', 51.195, 143.12, 21.0, 'Far_East_Transit', 'Gazprom', 'KS_44_Sakhalin_Booster', [143.1, 51.18, 143.14, 51.21]),
    _cs('CS-045', 'Khabarovskaya', 48.435, 135.17, 21.5, 'Far_East_Transit', 'Gazprom', 'KS_45_Khabarovskaya', [135.15, 48.42, 135.19, 48.45]),
    _cs('CS-046', 'Vladivostok Terminal', 43.135, 131.92, 22.0, 'Far_East_Transit', 'Gazprom', 'KS_46_Vladivostok_Terminal', [131.9, 43.12, 131.94, 43.15]),
    _cs('CS-047', 'Dombarovskaya Orenburg', 50.765, 59.52, 22.5, 'Far_East_Transit', 'Gazprom', 'KS_47_Dombarovskaya_Orenburg', [59.5, 50.75, 59.54, 50.78]),
    _cs('CS-048', 'AleksandrovGay KazakhBorder', 50.165, 48.57, 23.0, 'Far_East_Transit', 'Gazprom', 'KS_48_AleksandrovGay_KazakhBorder', [48.55, 50.15, 48.59, 50.18]),
    _cs('CS-049', 'Kasimov UndergroundStorage', 54.935, 41.4, 20.0, 'Far_East_Transit', 'Gazprom', 'KS_49_Kasimov_UndergroundStorage', [41.38, 54.92, 41.42, 54.95]),
    _cs('CS-050', 'Nevinnomyssk NorthCaucasus', 44.635, 41.94, 20.5, 'Far_East_Transit', 'Gazprom', 'KS_50_Nevinnomyssk_NorthCaucasus', [41.92, 44.62, 41.96, 44.65]),
    _cs('CS-051', 'Sakhalin OBTG', 51.195, 143.14, 21.0, 'Far_East_Sakhalin', 'Gazprom', 'GKS_01_Sakhalin_OBTG', [143.12, 51.18, 143.16, 51.21]),
    _cs('CS-052', 'DeKastri Khabarovsk', 51.465, 140.72, 21.5, 'Far_East_Sakhalin', 'Gazprom', 'KS_02_DeKastri_Khabarovsk', [140.7, 51.45, 140.74, 51.48]),
    _cs('CS-053', 'Tsimmermanovskaya', 51.365, 139.12, 22.0, 'Far_East_Sakhalin', 'Gazprom', 'KS_03_Tsimmermanovskaya', [139.1, 51.35, 139.14, 51.38]),
    _cs('CS-054', 'Khabarovskaya Hub', 48.395, 135.07, 22.5, 'Far_East_Sakhalin', 'Gazprom', 'KS_04_Khabarovskaya_Hub', [135.05, 48.38, 135.09, 48.41]),
    _cs('CS-055', 'Vyazemskaya Primorye', 47.535, 134.74, 23.0, 'Far_East_Sakhalin', 'Gazprom', 'KS_05_Vyazemskaya_Primorye', [134.72, 47.52, 134.76, 47.55]),
    _cs('CS-056', 'Dalnerechenskaya', 45.915, 133.72, 20.0, 'Far_East_Sakhalin', 'Gazprom', 'KS_06_Dalnerechenskaya', [133.7, 45.9, 133.74, 45.93]),
    _cs('CS-057', 'Ussuriyskaya', 43.815, 131.94, 20.5, 'Far_East_Sakhalin', 'Gazprom', 'KS_07_Ussuriyskaya', [131.92, 43.8, 131.96, 43.83]),
    _cs('CS-058', 'Nadezhdinskaya Vladivostok', 43.395, 132.0, 21.0, 'Far_East_Sakhalin', 'Gazprom', 'KS_08_Nadezhdinskaya_Vladivostok', [131.98, 43.38, 132.02, 43.41]),
    _cs('CS-059', 'Volkhov Nord Hub', 59.915, 32.34, 19.5, 'RU_Extended_NW', 'Gazprom', 'KS_09_Volkhov_Nord_Hub', [32.32, 59.9, 32.36, 59.93]),
    _cs('CS-060', 'Babaevo Northern Lights', 59.395, 35.94, 20.0, 'RU_Extended_NW', 'Gazprom', 'KS_10_Babaevo_Northern_Lights', [35.92, 59.38, 35.96, 59.41]),
    _cs('CS-061', 'Torzhok South', 57.015, 34.92, 20.5, 'RU_Extended_NW', 'Gazprom', 'KS_11_Torzhok_South', [34.9, 57.0, 34.94, 57.03]),
    _cs('CS-062', 'Valday Tver', 57.995, 33.24, 21.0, 'RU_Extended_NW', 'Gazprom', 'KS_12_Valday_Tver', [33.22, 57.98, 33.26, 58.01]),
    _cs('CS-063', 'Rzhev Transit', 56.265, 34.32, 18.0, 'RU_Extended_NW', 'Gazprom', 'KS_13_Rzhev_Transit', [34.3, 56.25, 34.34, 56.28]),
    _cs('CS-064', 'Kholmogorskaya Arkhangelsk', 64.235, 41.62, 18.5, 'RU_Extended_NW', 'Gazprom', 'KS_14_Kholmogorskaya_Arkhangelsk', [41.6, 64.22, 41.64, 64.25]),
    _cs('CS-065', 'Nyandoma North', 61.695, 40.14, 19.0, 'RU_Extended_NW', 'Gazprom', 'KS_15_Nyandoma_North', [40.12, 61.68, 40.16, 61.71]),
    _cs('CS-066', 'Krasnodarskaya', 45.095, 38.94, 19.5, 'Black_Sea_Azov', 'Gazprom', 'KS_16_Krasnodarskaya', [38.92, 45.08, 38.96, 45.11]),
    _cs('CS-067', 'Kubanskaya', 45.235, 38.12, 20.0, 'Black_Sea_Azov', 'Gazprom', 'KS_17_Kubanskaya', [38.1, 45.22, 38.14, 45.25]),
    _cs('CS-068', 'Anapskaya BlackSea', 44.915, 37.4, 20.5, 'Black_Sea_Azov', 'Gazprom', 'KS_18_Anapskaya_BlackSea', [37.38, 44.9, 37.42, 44.93]),
    _cs('CS-069', 'Timashevsk South', 45.635, 38.94, 21.0, 'Black_Sea_Azov', 'Gazprom', 'KS_19_Timashevsk_South', [38.92, 45.62, 38.96, 45.65]),
    _cs('CS-070', 'Sal Rostov', 47.115, 41.52, 18.0, 'Black_Sea_Azov', 'Gazprom', 'KS_20_Sal_Rostov', [41.5, 47.1, 41.54, 47.13]),
    _cs('CS-071', 'Bovanenkovo Yamal', 70.335, 68.82, 25.5, 'Yamal_Gydan_Field', 'Gazprom', 'GKS_21_Bovanenkovo_Yamal', [68.8, 70.32, 68.84, 70.35]),
    _cs('CS-072', 'Yamburg Field', 67.915, 75.04, 26.0, 'Yamal_Gydan_Field', 'Gazprom', 'GKS_22_Yamburg_Field', [75.02, 67.9, 75.06, 67.93]),
    _cs('CS-073', 'Zapolyarnaya', 66.915, 80.84, 26.5, 'Yamal_Gydan_Field', 'Gazprom', 'GKS_23_Zapolyarnaya', [80.82, 66.9, 80.86, 66.93]),
    _cs('CS-074', 'Kharasavey Yamal', 71.195, 66.77, 27.0, 'Yamal_Gydan_Field', 'Gazprom', 'GKS_24_Kharasavey_Yamal', [66.75, 71.18, 66.79, 71.21]),
    _cs('CS-075', 'Utrenny Utrennee LNG', 71.265, 75.72, 27.5, 'Yamal_Gydan_Field', 'Gazprom', 'GKS_25_Utrenny_Utrennee_LNG', [75.7, 71.25, 75.74, 71.28]),
    _cs('CS-076', 'LongYugan', 65.195, 72.24, 25.0, 'West_Siberia_Ural', 'Gazprom', 'KS_26_LongYugan', [72.22, 65.18, 72.26, 65.21]),
    _cs('CS-077', 'Pravohettinskaya', 65.415, 73.52, 22.0, 'West_Siberia_Ural', 'Gazprom', 'KS_27_Pravohettinskaya', [73.5, 65.4, 73.54, 65.43]),
    _cs('CS-078', 'Priobskaya', 61.135, 67.12, 22.5, 'West_Siberia_Ural', 'Gazprom', 'KS_28_Priobskaya', [67.1, 61.12, 67.14, 61.15]),
    _cs('CS-079', 'Kazym Yugra', 63.635, 66.22, 23.0, 'West_Siberia_Ural', 'Gazprom', 'KS_29_Kazym_Yugra', [66.2, 63.62, 66.24, 63.65]),
    _cs('CS-080', 'Otyadya Khanty', 61.815, 64.22, 23.5, 'West_Siberia_Ural', 'Gazprom', 'KS_30_Otyadya_Khanty', [64.2, 61.8, 64.24, 61.83]),
    _cs('CS-081', 'Saranpaul Ural', 64.265, 60.92, 24.0, 'West_Siberia_Ural', 'Gazprom', 'KS_31_Saranpaul_Ural', [60.9, 64.25, 60.94, 64.28]),
    _cs('CS-082', 'Peschany Umet UGS', 51.595, 45.84, 17.5, 'Volga_UGS', 'Gazprom', 'KS_32_Peschany_Umet_UGS', [45.82, 51.58, 45.86, 51.61]),
    _cs('CS-083', 'Stepnovskaya UGS', 51.395, 46.82, 18.0, 'Volga_UGS', 'Gazprom', 'KS_33_Stepnovskaya_UGS', [46.8, 51.38, 46.84, 51.41]),
    _cs('CS-084', 'Kanchurinskaya UGS', 52.765, 55.84, 15.0, 'Volga_UGS', 'Gazprom', 'KS_34_Kanchurinskaya_UGS', [55.82, 52.75, 55.86, 52.78]),
    _cs('CS-085', 'Sovhoznoe UGS', 51.815, 55.12, 15.5, 'Volga_UGS', 'Gazprom', 'KS_35_Sovhoznoe_UGS', [55.1, 51.8, 55.14, 51.83]),
    _cs('CS-086', 'Saratov Hub', 51.535, 46.02, 16.0, 'Volga_UGS', 'Gazprom', 'KS_36_Saratov_Hub', [46.0, 51.52, 46.04, 51.55]),
    _cs('CS-087', 'Tolyatti Samara', 53.535, 49.42, 16.5, 'Volga_UGS', 'Gazprom', 'KS_37_Tolyatti_Samara', [49.4, 53.52, 49.44, 53.55]),
    _cs('CS-088', 'Almetievsk Tatarstan', 54.915, 52.32, 17.0, 'Volga_UGS', 'Gazprom', 'KS_38_Almetievsk_Tatarstan', [52.3, 54.9, 52.34, 54.93]),
    _cs('CS-089', 'Pervomayskaya Tula', 54.035, 37.54, 17.5, 'Volga_UGS', 'Gazprom', 'KS_39_Pervomayskaya_Tula', [37.52, 54.02, 37.56, 54.05]),
    _cs('CS-090', 'Kramatorskaya Orel', 52.965, 36.02, 18.0, 'Volga_UGS', 'Gazprom', 'KS_40_Kramatorskaya_Orel', [36.0, 52.95, 36.04, 52.98]),
    _cs('CS-091', 'Proskokovo Kemerovo', 55.835, 85.64, 16.0, 'Siberia_Internal', 'Gazprom', 'KS_41_Proskokovo_Kemerovo', [85.62, 55.82, 85.66, 55.85]),
    _cs('CS-092', 'Barabinsk Novosibirsk', 55.365, 78.37, 16.5, 'Siberia_Internal', 'Gazprom', 'KS_42_Barabinsk_Novosibirsk', [78.35, 55.35, 78.39, 55.38]),
    _cs('CS-093', 'Omsk Transit', 54.995, 73.32, 17.0, 'Siberia_Internal', 'Gazprom', 'KS_43_Omsk_Transit', [73.3, 54.98, 73.34, 55.01]),
    _cs('CS-094', 'Tomsk Hub', 56.515, 84.97, 17.5, 'Siberia_Internal', 'Gazprom', 'KS_44_Tomsk_Hub', [84.95, 56.5, 84.99, 56.53]),
    _cs('CS-095', 'Novokuznetsk', 53.765, 87.12, 18.0, 'Siberia_Internal', 'Gazprom', 'KS_45_Novokuznetsk', [87.1, 53.75, 87.14, 53.78]),
    _cs('CS-096', 'Astrakhanskaya GPP', 46.735, 48.1, 16.5, 'Caspian_Astrakhan', 'Gazprom', 'KS_46_Astrakhanskaya_GPP', [48.08, 46.72, 48.12, 46.75]),
    _cs('CS-097', 'Zambyn Kalmykia', 46.235, 45.22, 17.0, 'Caspian_Astrakhan', 'Gazprom', 'KS_47_Zambyn_Kalmykia', [45.2, 46.22, 45.24, 46.25]),
    _cs('CS-098', 'Makarovo Bashkiria', 53.615, 56.22, 14.0, 'Caspian_Astrakhan', 'Gazprom', 'KS_48_Makarovo_Bashkiria', [56.2, 53.6, 56.24, 53.63]),
    _cs('CS-099', 'Chishmy Ufa', 54.615, 55.4, 14.5, 'Caspian_Astrakhan', 'Gazprom', 'KS_49_Chishmy_Ufa', [55.38, 54.6, 55.42, 54.63]),
    _cs('CS-100', 'Sharkan Udmurtia', 57.165, 53.9, 15.0, 'Caspian_Astrakhan', 'Gazprom', 'KS_50_Sharkan_Udmurtia', [53.88, 57.15, 53.92, 57.18]),
    _cs('CS-101', 'Malay Export Hub', 38.815, 63.4, 31.5, 'Turkmenistan_CAC_China', 'Turkmengaz', 'KS_01_Malay_Export_Hub', [63.38, 38.8, 63.42, 38.83]),
    _cs('CS-102', 'Galkynysh Central 1', 37.395, 62.22, 32.0, 'Turkmenistan_CAC_China', 'Turkmengaz', 'GSP_02_Galkynysh_Central_1', [62.2, 37.38, 62.24, 37.41]),
    _cs('CS-103', 'Galkynysh Central 2', 37.465, 62.27, 32.5, 'Turkmenistan_CAC_China', 'Turkmengaz', 'GSP_03_Galkynysh_Central_2', [62.25, 37.45, 62.29, 37.48]),
    _cs('CS-104', 'Galkynysh Central 3', 37.535, 62.32, 33.0, 'Turkmenistan_CAC_China', 'Turkmengaz', 'GSP_04_Galkynysh_Central_3', [62.3, 37.52, 62.34, 37.55]),
    _cs('CS-105', 'Bagtyyarlyk GPP 1', 38.435, 63.8, 30.0, 'Turkmenistan_CAC_China', 'Turkmengaz', 'GSP_05_Bagtyyarlyk_GPP_1', [63.78, 38.42, 63.82, 38.45]),
    _cs('CS-106', 'Bagtyyarlyk GPP 2', 38.565, 63.9, 30.5, 'Turkmenistan_CAC_China', 'Turkmengaz', 'GSP_06_Bagtyyarlyk_GPP_2', [63.88, 38.55, 63.92, 38.58]),
    _cs('CS-107', 'Gedai Border Transit', 38.995, 63.97, 31.0, 'Turkmenistan_CAC_China', 'Turkmengaz', 'KS_07_Gedai_Border_Transit', [63.95, 38.98, 63.99, 39.01]),
    _cs('CS-108', 'Samandepe Booster', 38.315, 63.74, 31.5, 'Turkmenistan_CAC_China', 'Turkmengaz', 'KS_08_Samandepe_Booster', [63.72, 38.3, 63.76, 38.33]),
    _cs('CS-109', 'Farap Border North', 39.135, 63.62, 32.0, 'Turkmenistan_CAC_China', 'Turkmengaz', 'KS_09_Farap_Border_North', [63.6, 39.12, 63.64, 39.15]),
    _cs('CS-110', 'Shatlyk Main Hub', 37.595, 61.92, 37.5, 'Turkmenistan_Galkynysh', 'Turkmengaz', 'KS_10_Shatlyk_Main_Hub', [61.9, 37.58, 61.94, 37.61]),
    _cs('CS-111', 'Yashlar Field', 37.215, 62.12, 38.0, 'Turkmenistan_Galkynysh', 'Turkmengaz', 'GSP_11_Yashlar_Field', [62.1, 37.2, 62.14, 37.23]),
    _cs('CS-112', 'Garakel Field', 37.065, 62.07, 35.0, 'Turkmenistan_Galkynysh', 'Turkmengaz', 'GSP_12_Garakel_Field', [62.05, 37.05, 62.09, 37.08]),
    _cs('CS-113', 'Mary GRES Supply', 37.695, 61.82, 35.5, 'Turkmenistan_Galkynysh', 'Turkmengaz', 'KS_13_Mary_GRES_Supply', [61.8, 37.68, 61.84, 37.71]),
    _cs('CS-114', 'Bayramali Central', 37.635, 62.17, 36.0, 'Turkmenistan_Galkynysh', 'Turkmengaz', 'KS_14_Bayramali_Central', [62.15, 37.62, 62.19, 37.65]),
    _cs('CS-115', 'Guranly Field', 37.315, 61.77, 36.5, 'Turkmenistan_Galkynysh', 'Turkmengaz', 'GSP_15_Guranly_Field', [61.75, 37.3, 61.79, 37.33]),
    _cs('CS-116', 'EastWest GKS Shatlyk', 37.615, 61.9, 30.0, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_16_EastWest_GKS_Shatlyk', [61.88, 37.6, 61.92, 37.63]),
    _cs('CS-117', 'EastWest KS1 Tejen', 37.435, 60.52, 30.5, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_17_EastWest_KS1_Tejen', [60.5, 37.42, 60.54, 37.45]),
    _cs('CS-118', 'EastWest KS2 Dushak', 37.165, 59.62, 31.0, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_18_EastWest_KS2_Dushak', [59.6, 37.15, 59.64, 37.18]),
    _cs('CS-119', 'EastWest KS3 Abadan', 38.065, 58.22, 28.0, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_19_EastWest_KS3_Abadan', [58.2, 38.05, 58.24, 38.08]),
    _cs('CS-120', 'EastWest KS4 Gokdepe', 38.195, 57.92, 28.5, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_20_EastWest_KS4_Gokdepe', [57.9, 38.18, 57.94, 38.21]),
    _cs('CS-121', 'EastWest KS5 Baharli', 38.465, 57.32, 29.0, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_21_EastWest_KS5_Baharli', [57.3, 38.45, 57.34, 38.48]),
    _cs('CS-122', 'EastWest KS6 Serdar', 38.995, 56.3, 29.5, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_22_EastWest_KS6_Serdar', [56.28, 38.98, 56.32, 39.01]),
    _cs('CS-123', 'EastWest KS7 Bereket', 39.235, 55.52, 30.0, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_23_EastWest_KS7_Bereket', [55.5, 39.22, 55.54, 39.25]),
    _cs('CS-124', 'EastWest KS8 Belek', 39.915, 53.82, 30.5, 'Turkmenistan_East_West', 'Turkmengaz', 'KS_24_EastWest_KS8_Belek', [53.8, 39.9, 53.84, 39.93]),
    _cs('CS-125', 'Korpedje Iran Export', 37.795, 54.22, 15.0, 'Turkmenistan_Caspian', 'Turkmengaz', 'KS_25_Korpedje_Iran_Export', [54.2, 37.78, 54.24, 37.81]),
    _cs('CS-126', 'Etrek Iran Border', 37.395, 54.07, 12.0, 'Turkmenistan_Caspian', 'Turkmengaz', 'KS_26_Etrek_Iran_Border', [54.05, 37.38, 54.09, 37.41]),
    _cs('CS-127', 'Cheleken DragonOil', 39.435, 53.17, 12.5, 'Turkmenistan_Caspian', 'Turkmengaz', 'GSP_27_Cheleken_DragonOil', [53.15, 39.42, 53.19, 39.45]),
    _cs('CS-128', 'Kiyanly Petronas GPP', 40.365, 52.94, 13.0, 'Turkmenistan_Caspian', 'Turkmengaz', 'GSP_28_Kiyanly_Petronas_GPP', [52.92, 40.35, 52.96, 40.38]),
    _cs('CS-129', 'Turkmenbashi Refinery', 40.035, 53.02, 13.5, 'Turkmenistan_Caspian', 'Turkmengaz', 'KS_29_Turkmenbashi_Refinery', [53.0, 40.02, 53.04, 40.05]),
    _cs('CS-130', 'Goturdepe Field', 39.665, 53.82, 14.0, 'Turkmenistan_Caspian', 'Turkmengaz', 'GSP_30_Goturdepe_Field', [53.8, 39.65, 53.84, 39.68]),
    _cs('CS-131', 'Barsagelmes Field', 39.535, 53.97, 14.5, 'Turkmenistan_Caspian', 'Turkmengaz', 'GSP_31_Barsagelmes_Field', [53.95, 39.52, 53.99, 39.55]),
    _cs('CS-132', 'Gamyshlyja Field', 38.115, 54.12, 15.0, 'Turkmenistan_Caspian', 'Turkmengaz', 'GSP_32_Gamyshlyja_Field', [54.1, 38.1, 54.14, 38.13]),
    _cs('CS-133', 'Deryalyk CAC Export', 41.835, 59.82, 20.0, 'Turkmenistan_CAC_North', 'Turkmengaz', 'KS_33_Deryalyk_CAC_Export', [59.8, 41.82, 59.84, 41.85]),
    _cs('CS-134', 'Naiip GPP Hub', 40.265, 59.42, 20.5, 'Turkmenistan_CAC_North', 'Turkmengaz', 'KS_34_Naiip_GPP_Hub', [59.4, 40.25, 59.44, 40.28]),
    _cs('CS-135', 'GazliAvchak CAC', 41.115, 59.22, 21.0, 'Turkmenistan_CAC_North', 'Turkmengaz', 'KS_35_GazliAvchak_CAC', [59.2, 41.1, 59.24, 41.13]),
    _cs('CS-136', 'Sakar Field', 38.915, 63.24, 21.5, 'Turkmenistan_CAC_North', 'Turkmengaz', 'GSP_36_Sakar_Field', [63.22, 38.9, 63.26, 38.93]),
    _cs('CS-137', 'Gubadag North', 41.935, 59.97, 22.0, 'Turkmenistan_CAC_North', 'Turkmengaz', 'GSP_37_Gubadag_North', [59.95, 41.92, 59.99, 41.95]),
    _cs('CS-138', 'Dashoguz North', 41.815, 59.14, 22.5, 'Turkmenistan_CAC_North', 'Turkmengaz', 'KS_38_Dashoguz_North', [59.12, 41.8, 59.16, 41.83]),
    _cs('CS-139', 'Zeagli Darvaza Central', 40.215, 58.42, 13.0, 'Turkmenistan_Karaktum', 'Turkmengaz', 'GSP_39_Zeagli_Darvaza_Central', [58.4, 40.2, 58.44, 40.23]),
    _cs('CS-140', 'Achak CAC Booster', 41.265, 59.12, 10.0, 'Turkmenistan_Karaktum', 'Turkmengaz', 'KS_40_Achak_CAC_Booster', [59.1, 41.25, 59.14, 41.28]),
    _cs('CS-141', 'Gazlydepe Field', 38.115, 63.52, 10.5, 'Turkmenistan_Karaktum', 'Turkmengaz', 'GSP_41_Gazlydepe_Field', [63.5, 38.1, 63.54, 38.13]),
    _cs('CS-142', 'Pelvert Field', 38.615, 64.12, 11.0, 'Turkmenistan_Karaktum', 'Turkmengaz', 'GSP_42_Pelvert_Field', [64.1, 38.6, 64.14, 38.63]),
    _cs('CS-143', 'Maldar Field', 38.215, 63.12, 11.5, 'Turkmenistan_Karaktum', 'Turkmengaz', 'GSP_43_Maldar_Field', [63.1, 38.2, 63.14, 38.23]),
    _cs('CS-144', 'TAPI GKS Galkynysh', 37.315, 62.37, 35.0, 'TAPI_Corridor', 'Turkmengaz', 'KS_44_TAPI_GKS_Galkynysh', [62.35, 37.3, 62.39, 37.33]),
    _cs('CS-145', 'Serhetabat Afghan Border', 35.295, 62.34, 35.5, 'TAPI_Corridor', 'Turkmengaz', 'KS_45_Serhetabat_Afghan_Border', [62.32, 35.28, 62.36, 35.31]),
    _cs('CS-146', 'Garabogaz Carbamide', 41.535, 52.54, 11.0, 'Turkmenistan_GCC', 'Turkmengaz', 'GCC_46_Garabogaz_Carbamide', [52.52, 41.52, 52.56, 41.55]),
    _cs('CS-147', 'Kiyanly Polymer', 40.335, 53.0, 8.0, 'Turkmenistan_GCC', 'Turkmengaz', 'GCC_47_Kiyanly_Polymer', [52.98, 40.32, 53.02, 40.35]),
    _cs('CS-148', 'OvadanDepe GTG', 38.165, 58.37, 8.5, 'Turkmenistan_GCC', 'Turkmengaz', 'GCC_48_OvadanDepe_GTG', [58.35, 38.15, 58.39, 38.18]),
    _cs('CS-149', 'Tedjen Carbamide', 37.395, 60.5, 9.0, 'Turkmenistan_GCC', 'Turkmengaz', 'KS_49_Tedjen_Carbamide', [60.48, 37.38, 60.52, 37.41]),
    _cs('CS-150', 'Seydi Refinery', 39.495, 62.92, 9.5, 'Turkmenistan_GCC', 'Turkmengaz', 'KS_50_Seydi_Refinery', [62.9, 39.48, 62.94, 39.51]),
    _cs('CS-151', 'Horgos Border Hub', 44.215, 80.42, 42.0, 'China_Xinjiang_CAC', 'PipeChina', 'KS_01_Horgos_Border_Hub', [80.4, 44.2, 80.44, 44.23]),
    _cs('CS-152', 'Horgos GKS Main', 44.195, 80.47, 42.5, 'China_Xinjiang_CAC', 'PipeChina', 'KS_02_Horgos_GKS_Main', [80.45, 44.18, 80.49, 44.21]),
    _cs('CS-153', 'Tarim Lunnan Hub', 41.735, 84.27, 43.0, 'China_Xinjiang_CAC', 'PipeChina', 'GSP_03_Tarim_Lunnan_Hub', [84.25, 41.72, 84.29, 41.75]),
    _cs('CS-154', 'Kuerle Booster', 41.765, 86.14, 40.0, 'China_Xinjiang_CAC', 'PipeChina', 'KS_04_Kuerle_Booster', [86.12, 41.75, 86.16, 41.78]),
    _cs('CS-155', 'Shanshan Central', 42.865, 90.3, 40.5, 'China_Xinjiang_CAC', 'PipeChina', 'KS_05_Shanshan_Central', [90.28, 42.85, 90.32, 42.88]),
    _cs('CS-156', 'Yumen Main', 40.295, 97.04, 36.0, 'China_Hexi', 'PipeChina', 'KS_06_Yumen_Main', [97.02, 40.28, 97.06, 40.31]),
    _cs('CS-157', 'Zhangye Booster', 38.935, 100.47, 36.5, 'China_Hexi', 'PipeChina', 'KS_07_Zhangye_Booster', [100.45, 38.92, 100.49, 38.95]),
    _cs('CS-158', 'Lanzhou Junction', 36.065, 103.84, 37.0, 'China_Hexi', 'PipeChina', 'KS_08_Lanzhou_Junction', [103.82, 36.05, 103.86, 36.08]),
    _cs('CS-159', 'Zhongwei National Hub', 37.515, 105.2, 37.5, 'China_Hexi', 'PipeChina', 'KS_09_Zhongwei_National_Hub', [105.18, 37.5, 105.22, 37.53]),
    _cs('CS-160', 'Changqing Jingbian', 37.615, 108.8, 28.0, 'China_Ordos_Central', 'PipeChina', 'GSP_10_Changqing_Jingbian', [108.78, 37.6, 108.82, 37.63]),
    _cs('CS-161', 'Yulin Booster', 38.295, 109.74, 25.0, 'China_Ordos_Central', 'PipeChina', 'KS_11_Yulin_Booster', [109.72, 38.28, 109.76, 38.31]),
    _cs('CS-162', 'Suqiao Storage Hub', 39.035, 116.9, 25.5, 'China_Ordos_Central', 'PipeChina', 'UGS_12_Suqiao_Storage_Hub', [116.88, 39.02, 116.92, 39.05]),
    _cs('CS-163', 'Taiyuan Central', 37.865, 112.54, 26.0, 'China_Ordos_Central', 'PipeChina', 'KS_13_Taiyuan_Central', [112.52, 37.85, 112.56, 37.88]),
    _cs('CS-164', 'Beijing Dagang Hub', 38.835, 117.37, 21.5, 'China_Capital_NE', 'PipeChina', 'KS_14_Beijing_Dagang_Hub', [117.35, 38.82, 117.39, 38.85]),
    _cs('CS-165', 'Yanshan Petrochem', 39.715, 115.97, 22.0, 'China_Capital_NE', 'PipeChina', 'KS_15_Yanshan_Petrochem', [115.95, 39.7, 115.99, 39.73]),
    _cs('CS-166', 'Tianjin LNG Terminal', 38.995, 117.72, 22.5, 'China_Capital_NE', 'PipeChina', 'GCC_16_Tianjin_LNG_Terminal', [117.7, 38.98, 117.74, 39.01]),
    _cs('CS-167', 'Nanjing Distribution', 32.065, 118.8, 25.0, 'China_East', 'PipeChina', 'KS_17_Nanjing_Distribution', [118.78, 32.05, 118.82, 32.08]),
    _cs('CS-168', 'Shanghai Baihedun Hub', 31.195, 121.12, 22.0, 'China_East', 'PipeChina', 'KS_18_Shanghai_Baihedun_Hub', [121.1, 31.18, 121.14, 31.21]),
    _cs('CS-169', 'Hangzhou Terminal', 30.295, 120.17, 22.5, 'China_East', 'PipeChina', 'KS_19_Hangzhou_Terminal', [120.15, 30.28, 120.19, 30.31]),
    _cs('CS-170', 'Ningbo LNG Hub', 29.915, 121.87, 23.0, 'China_East', 'PipeChina', 'GCC_20_Ningbo_LNG_Hub', [121.85, 29.9, 121.89, 29.93]),
    _cs('CS-171', 'Guilin Booster', 25.265, 110.3, 19.5, 'China_South', 'PipeChina', 'KS_21_Guilin_Booster', [110.28, 25.25, 110.32, 25.28]),
    _cs('CS-172', 'Guangzhou Main Hub', 23.135, 113.27, 20.0, 'China_South', 'PipeChina', 'KS_22_Guangzhou_Main_Hub', [113.25, 23.12, 113.29, 23.15]),
    _cs('CS-173', 'Shenzhen Dapeng Hub', 22.615, 114.5, 20.5, 'China_South', 'PipeChina', 'KS_23_Shenzhen_Dapeng_Hub', [114.48, 22.6, 114.52, 22.63]),
    _cs('CS-174', 'Nanning Terminal', 22.815, 108.34, 21.0, 'China_South', 'PipeChina', 'KS_24_Nanning_Terminal', [108.32, 22.8, 108.36, 22.83]),
    _cs('CS-175', 'Zhuhai LNG Terminal', 21.965, 113.22, 18.0, 'China_South', 'PipeChina', 'GCC_25_Zhuhai_LNG_Terminal', [113.2, 21.95, 113.24, 21.98]),
    _cs('CS-176', 'Anyue Gas Field', 30.115, 105.34, 24.5, 'China_Sichuan', 'PipeChina', 'GSP_26_Anyue_Gas_Field', [105.32, 30.1, 105.36, 30.13]),
    _cs('CS-177', 'Fuling Shale Gas', 29.715, 107.42, 25.0, 'China_Sichuan', 'PipeChina', 'GSP_27_Fuling_Shale_Gas', [107.4, 29.7, 107.44, 29.73]),
    _cs('CS-178', 'Chengdu Distribution', 30.665, 104.08, 25.5, 'China_Sichuan', 'PipeChina', 'KS_28_Chengdu_Distribution', [104.06, 30.65, 104.1, 30.68]),
    _cs('CS-179', 'Zhongxian Booster', 30.315, 108.04, 26.0, 'China_Sichuan', 'PipeChina', 'KS_29_Zhongxian_Booster', [108.02, 30.3, 108.06, 30.33]),
    _cs('CS-180', 'Wuhan Central Hub', 30.595, 114.32, 26.5, 'China_Sichuan', 'PipeChina', 'KS_30_Wuhan_Central_Hub', [114.3, 30.58, 114.34, 30.61]),
    _cs('CS-181', 'Heihe Border Import', 50.235, 127.52, 41.0, 'China_NE_Import', 'PipeChina', 'KS_31_Heihe_Border_Import', [127.5, 50.22, 127.54, 50.25]),
    _cs('CS-182', 'Harbin Distribution', 45.765, 126.65, 38.0, 'China_NE_Import', 'PipeChina', 'KS_32_Harbin_Distribution', [126.63, 45.75, 126.67, 45.78]),
    _cs('CS-183', 'Shenyang Booster', 41.815, 123.44, 38.5, 'China_NE_Import', 'PipeChina', 'KS_33_Shenyang_Booster', [123.42, 41.8, 123.46, 41.83]),
    _cs('CS-184', 'Liaohe Storage Hub', 41.135, 122.12, 39.0, 'China_NE_Import', 'PipeChina', 'UGS_34_Liaohe_Storage_Hub', [122.1, 41.12, 122.14, 41.15]),
    _cs('CS-185', 'Anshan Steel Supply', 41.115, 123.0, 39.5, 'China_NE_Import', 'PipeChina', 'KS_35_Anshan_Steel_Supply', [122.98, 41.1, 123.02, 41.13]),
)

# Legacy bbox cluster dicts — single catalog SoT at repo-root compressor_stations.py
# (API geometry remains frozen ALL_COMPRESSOR_STATIONS tuples above).
import compressor_stations as _bbox_catalog

COMPRESSOR_STATIONS = _bbox_catalog.COMPRESSOR_STATIONS
ADDITIONAL_COMPRESSOR_STATIONS = _bbox_catalog.ADDITIONAL_COMPRESSOR_STATIONS
TURKMENISTAN_COMPRESSOR_STATIONS = _bbox_catalog.TURKMENISTAN_COMPRESSOR_STATIONS
CHINA_COMPRESSOR_STATIONS = _bbox_catalog.CHINA_COMPRESSOR_STATIONS

COMPRESSOR_STATIONS_BY_ID: dict[str, CompressorStation] = {
    cs.id: cs for cs in ALL_COMPRESSOR_STATIONS
}
COMPRESSOR_STATIONS_BY_LEGACY: dict[str, CompressorStation] = {
    cs.legacy_id: cs for cs in ALL_COMPRESSOR_STATIONS if cs.legacy_id
}


def haversine_nm(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in nautical miles (1 km = 0.539957 nm)."""
    r_km = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))
    return r_km * c * 0.539957


def get_all_stations() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for cs in ALL_COMPRESSOR_STATIONS:
        row: dict[str, Any] = {
            "id": cs.id,
            "name": cs.name,
            "lat": cs.lat,
            "lon": cs.lon,
            "capacity_bcm_y": cs.capacity_bcm_y,
            "corridor": cs.corridor,
            "operator": cs.operator,
            "legacy_id": cs.legacy_id,
        }
        if cs.bbox is not None:
            row["bbox"] = list(cs.bbox)
        out.append(row)
    return out


def stations_at_point(lat: float, lon: float) -> list[dict[str, Any]]:
    """Point-in-bbox hits via ``services.spatial_index`` (O(1) avg)."""
    from services.spatial_index import stations_containing_point

    try:
        lat_f = float(lat)
        lon_f = float(lon)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(lat_f) or not math.isfinite(lon_f):
        return []
    hits = stations_containing_point(lon_f, lat_f)
    out: list[dict[str, Any]] = []
    for hit in hits:
        cs = COMPRESSOR_STATIONS_BY_LEGACY.get(hit.name)
        if cs is None:
            out.append(
                {
                    "legacy_id": hit.name,
                    "lat": hit.centroid[0],
                    "lon": hit.centroid[1],
                    "bbox": list(hit.bbox),
                    "cluster": hit.cluster,
                }
            )
            continue
        out.append(
            {
                "id": cs.id,
                "name": cs.name,
                "lat": cs.lat,
                "lon": cs.lon,
                "capacity_bcm_y": cs.capacity_bcm_y,
                "corridor": cs.corridor,
                "operator": cs.operator,
                "legacy_id": cs.legacy_id,
                "bbox": list(cs.bbox) if cs.bbox else list(hit.bbox),
            }
        )
    return out


def find_nearest_stations(
    lat: float,
    lon: float,
    max_distance_nm: float = 50.0,
) -> list[dict[str, Any]]:
    """Stations within buffer, sorted by distance_nm. Empty list if none / bad input.

    Candidate pruning uses the grid spatial index (expanded degree window),
    then exact Haversine filter — O(k) instead of full N when buffer is local.
    """
    results: list[dict[str, Any]] = []
    try:
        lat_f = float(lat)
        lon_f = float(lon)
        max_d = float(max_distance_nm)
    except (TypeError, ValueError):
        return []
    if not math.isfinite(lat_f) or not math.isfinite(lon_f):
        return []

    # ~1 nm ≈ 1/60° latitude; lon scale by cos(lat). Cap for polar safety.
    dlat = max_d / 60.0
    cos_lat = max(0.05, abs(math.cos(math.radians(lat_f))))
    dlon = dlat / cos_lat
    try:
        from services.spatial_index import get_spatial_index

        candidates = get_spatial_index().query_bbox(
            lon_f - dlon, lat_f - dlat, lon_f + dlon, lat_f + dlat
        )
        legacy_ids = {c.name for c in candidates}
        pool = [cs for cs in ALL_COMPRESSOR_STATIONS if cs.legacy_id in legacy_ids]
        # Safety: if index miss (empty) fall back to full scan.
        if not pool and ALL_COMPRESSOR_STATIONS:
            pool = list(ALL_COMPRESSOR_STATIONS)
    except Exception:  # noqa: BLE001
        pool = list(ALL_COMPRESSOR_STATIONS)

    for cs in pool:
        dist = haversine_nm(lat_f, lon_f, cs.lat, cs.lon)
        if dist <= max_d:
            results.append(
                {
                    "id": cs.id,
                    "name": cs.name,
                    "lat": cs.lat,
                    "lon": cs.lon,
                    "capacity_bcm_y": cs.capacity_bcm_y,
                    "corridor": cs.corridor,
                    "operator": cs.operator,
                    "legacy_id": cs.legacy_id,
                    "distance_nm": round(dist, 2),
                }
            )
    results.sort(key=lambda x: x["distance_nm"])
    return results


def build_gis_stations_payload() -> dict[str, Any]:
    stations = get_all_stations()
    n = len(stations)
    return {
        "contract_version": CONTRACT_VERSION,
        "count": n,
        "total_count": n,  # alias for orchestrator health probes (Contract 1.8.0)
        "stations": stations,
        "geometry": "point_centroid_of_bbox",
        "capacity_provenance": "corridor_class_notional_estimate",
    }


def enrich_route_position_proximity(
    lat: float,
    lon: float,
    *,
    max_distance_nm: float = 50.0,
) -> dict[str, Any]:
    """Attach proximity_compressors for a vessel position (never raises)."""
    try:
        prox = find_nearest_stations(lat, lon, max_distance_nm=max_distance_nm)
    except Exception:  # noqa: BLE001
        prox = []
    return {
        "lat": lat,
        "lon": lon,
        "proximity_compressors": prox,
        "proximity_buffer_nm": max_distance_nm,
        "contract_version": CONTRACT_VERSION,
    }


def analyze_route_payload(
    body: dict[str, Any] | None,
    *,
    max_distance_nm: float = 50.0,
) -> dict[str, Any]:
    """POST /api/v1/route/analytics — enrich vessel position(s) with proximity."""
    body = body if isinstance(body, dict) else {}
    buffer_nm = float(body.get("max_distance_nm") or max_distance_nm)
    vessels_in = body.get("vessels")
    out_vessels: list[dict[str, Any]] = []

    if isinstance(vessels_in, list) and vessels_in:
        for raw in vessels_in:
            if not isinstance(raw, dict):
                continue
            try:
                vlat = float(raw.get("lat"))
                vlon = float(raw.get("lon"))
            except (TypeError, ValueError):
                row = dict(raw)
                row["proximity_compressors"] = []
                out_vessels.append(row)
                continue
            prox = find_nearest_stations(vlat, vlon, max_distance_nm=buffer_nm)
            row = dict(raw)
            row["proximity_compressors"] = prox
            out_vessels.append(row)
        return {
            "contract_version": CONTRACT_VERSION,
            "proximity_buffer_nm": buffer_nm,
            "vessels": out_vessels,
            "proximity_compressors": out_vessels[0]["proximity_compressors"] if out_vessels else [],
        }

    # Single position
    try:
        lat = float(body.get("lat"))
        lon = float(body.get("lon"))
    except (TypeError, ValueError):
        return {
            "contract_version": CONTRACT_VERSION,
            "proximity_buffer_nm": buffer_nm,
            "proximity_compressors": [],
            "error": None,
            "note": "lat/lon missing or invalid — empty proximity",
        }

    prox = find_nearest_stations(lat, lon, max_distance_nm=buffer_nm)
    return {
        "contract_version": CONTRACT_VERSION,
        "lat": lat,
        "lon": lon,
        "proximity_buffer_nm": buffer_nm,
        "proximity_compressors": prox,
    }


def validate_registry() -> dict[str, Any]:
    assert len(ALL_COMPRESSOR_STATIONS) == 185, len(ALL_COMPRESSOR_STATIONS)
    ids = [cs.id for cs in ALL_COMPRESSOR_STATIONS]
    assert len(ids) == len(set(ids)), "duplicate station ids"
    for cs in ALL_COMPRESSOR_STATIONS:
        assert -90.0 <= cs.lat <= 90.0 and -180.0 <= cs.lon <= 180.0
        if cs.bbox is not None:
            lon_min, lat_min, lon_max, lat_max = cs.bbox
            assert lon_min <= lon_max and lat_min <= lat_max
    catalog_ok, catalog_n, catalog_errs = _bbox_catalog.validate_stations_registry()
    assert catalog_ok and catalog_n == 185, catalog_errs
    legacy = {cs.legacy_id for cs in ALL_COMPRESSOR_STATIONS}
    assert legacy == set(_bbox_catalog.ALL_COMPRESSOR_STATIONS.keys()), (
        "API SoT legacy_id set != root bbox catalog keys"
    )
    return {"ok": True, "count": 185, "contract_version": CONTRACT_VERSION}


validate_registry()


__all__ = (
    "CONTRACT_VERSION",
    "CompressorStation",
    "ALL_COMPRESSOR_STATIONS",
    "COMPRESSOR_STATIONS",
    "ADDITIONAL_COMPRESSOR_STATIONS",
    "TURKMENISTAN_COMPRESSOR_STATIONS",
    "CHINA_COMPRESSOR_STATIONS",
    "COMPRESSOR_STATIONS_BY_ID",
    "COMPRESSOR_STATIONS_BY_LEGACY",
    "haversine_nm",
    "get_all_stations",
    "stations_at_point",
    "find_nearest_stations",
    "build_gis_stations_payload",
    "enrich_route_position_proximity",
    "analyze_route_payload",
    "validate_registry",
)


if __name__ == "__main__":
    print(validate_registry())
    print("near Portovaya", find_nearest_stations(60.565, 28.05, 50.0)[:3])

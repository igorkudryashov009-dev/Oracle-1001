/**
 * ARCTIC Arc7 sheet (sheet id: arctic) — Christophe de Margerie + Georgiy Ushakov.
 *
 * Video provenance (ffprobe 2026-09-15): encoder Lavf58.76.100 only; no camera/GPS/
 * creation_time tags; *-flight naming matches LUMA/Q-Flex pattern → treat as
 * AI-GENERATED at the same trust level as Q-Flex LUMA REAL VIDEO track.
 * Dimensions: confirmed public registry / Q88 only — NEVER measured from video frames.
 */
export const ARCTIC_FLEET_BRAND = "ARCTIC · Arc7 Yamalmax";
export const ARCTIC_FLEET_SHORT = "ARCTIC";
export const ARCTIC_ASSET_BASE = "/assets/arctic";

/** Same trust language as Q-Flex LUMA REAL VIDEO track (top10_sheet.js LUMA_VIDEO_BADGE). */
export const ARCTIC_LUMA_VIDEO_BADGE =
  "AI-GENERATED VIDEO RECONSTRUCTION (LUMA.ai) · SPECULATIVE DETAIL · GEOMETRY AND MOTION NOT OSINT-VERIFIED";

export const ARCTIC_VIDEO_DERIVED_BADGE =
  "EXTRACTED FROM AI-GENERATED FLIGHT VIDEO · NOT INDEPENDENT PHOTOGRAPHS · NOT A VERIFIED MEASUREMENT SOURCE";

export const ARCTIC_TOP_VIEW_NOTE =
  "TOP VIEW UNAVAILABLE · FLIGHT PATH NEVER REACHES NADIR / OVERHEAD ON SOURCE VIDEO";

/** When a user-curated / extracted overhead still exists — still VIDEO-DERIVED, never Ortho. */
export const ARCTIC_TOP_VIEW_DERIVED_NOTE =
  "VIDEO-DERIVED OVERHEAD · AI FLIGHT STILL · NOT ORTHO / NOT A MEASUREMENT SOURCE";

export const ARCTIC_VESSELS = [
  {
    rank: 1,
    imo: "9737187",
    name: "CHRISTOPHE DE MARGERIE",
    class: "Arc7 / Yamalmax LNG",
    ice_class: "Arc7",
    flag: "Russian Federation",
    year_built: 2016,
    builder: "Daewoo Shipbuilding & Marine Engineering (DSME), Okpo",
    loa_m: 299.0,
    beam_m: 50.0,
    draft_m: 13.025,
    draft_kind: "summer_loadline",
    draft_status: "confirmed",
    design_draft_m: 11.7,
    dwt_tons: 96779,
    cargo_m3: 172600,
    particulars_sources: [
      {
        field: "IMO/LOA/Beam/DWT/summer draft 13.025 m",
        source: "INTERTANKO Q88 (via aukevisser.nl SCF Yamal / Christophe de Margerie)",
        url: "https://www.aukevisser.nl/supertankers/gas-1/id1112.htm",
      },
      {
        field: "design draught 11.7 m (class)",
        source: "Ship Technology — Christophe de Margerie Class; Railotech/Aker Arctic class sheet",
        url: "https://www.ship-technology.com/projects/christophe-de-margerie-class-icebreaking-lng-carriers/",
      },
      {
        field: "IMO 9737187 / Arc7 first-of-class",
        source: "ABB technical note 9AKK107045A7586 (SCF Yamal / Christophe de Margerie)",
        url: "https://search.abb.com/library/Download.aspx?Action=Launch&DocumentID=9AKK107045A7586",
      },
    ],
    flight_video: {
      ready: true,
      url: "/assets/arctic/videos/vessel_9737187.mp4",
      http_url: "/assets/arctic/videos/vessel_9737187.mp4",
      source_file: "chris-de-margerie-ice-free-arctic-flight.mp4",
      local_path: "C:\\111\\1001\\Artic\\chris-de-margerie-ice-free-arctic-flight.mp4",
      asset_source: "assets/arctic/videos/vessel_9737187_source.mp4",
      provider: "arctic_luma_flight",
      kind: "ai_generated_flight",
      provenance: "ai_generated",
      encoder_tag: "Lavf58.76.100",
      bytes: 890459,
      fidelity_badge: ARCTIC_LUMA_VIDEO_BADGE,
    },
    video_derived: {
      badge: ARCTIC_VIDEO_DERIVED_BADGE,
      selection: "user_curated_replace_agent_ffmpeg",
      top_available: true,
      top_note: ARCTIC_TOP_VIEW_DERIVED_NOTE,
      side: {
        label: "SIDE-OBLIQUE (user-curated)",
        url: "/assets/arctic/frames/vessel_9737187_side_oblique.jpg",
        fallback_url: "assets/arctic/frames/vessel_9737187_side_oblique.jpg",
        user_source: "chris-de-margerie-ice-free-arctic-flight-1-1.jpg",
        note: "Elevated starboard three-quarter — NOT true side ortho",
      },
      bow: {
        label: "BOW (user-curated)",
        url: "/assets/arctic/frames/vessel_9737187_bow_oblique.jpg",
        fallback_url: "assets/arctic/frames/vessel_9737187_bow_oblique.jpg",
        user_source: "chris-de-margerie-ice-free-arctic-flight-1-2.jpg",
        note: "Near head-on bow — still from AI flight, not photograph",
      },
      top: {
        label: "TOP / OVERHEAD (user-curated)",
        url: "/assets/arctic/frames/vessel_9737187_top_overhead.jpg",
        fallback_url: "assets/arctic/frames/vessel_9737187_top_overhead.jpg",
        user_source: "chris-de-margerie-ice-free-arctic-flight-1-3.jpg",
        note: ARCTIC_TOP_VIEW_DERIVED_NOTE,
      },
    },
    refs: {
      side: {
        label: "VIDEO-DERIVED · SIDE-OBLIQUE",
        url: "/assets/arctic/frames/vessel_9737187_side_oblique.jpg",
        fallback_url: "assets/arctic/frames/vessel_9737187_side_oblique.jpg",
      },
    },
  },
  {
    rank: 2,
    imo: "9750749",
    name: "GEORGIY USHAKOV",
    class: "Arc7 / Yamalmax LNG",
    ice_class: "Arc7",
    flag: "Bahamas",
    year_built: 2019,
    builder: "Daewoo Shipbuilding & Marine Engineering (DSME), Geoje (hull 2433)",
    loa_m: 299.0,
    beam_m: 50.0,
    draft_m: null,
    draft_kind: null,
    draft_status: "unconfirmed",
    draft_display: "не подтверждено",
    design_draft_m: 11.7,
    design_draft_note:
      "Class design draught 11.7 m (Christophe de Margerie class) — not a hull-specific summer Q88",
    dwt_tons: 96796,
    cargo_m3: 172600,
    particulars_sources: [
      {
        field: "IMO 9750749 / LOA 299 / Beam 50 / DWT 96796",
        source: "VesselFinder particulars; GUR war-sanctions registry; Maritime Optima",
        url: "https://www.vesselfinder.com/vessels/details/9750749",
      },
      {
        field: "Yard / class Arc7 Yamal 172.6",
        source: "FleetPhoto vessel 98086 (BV registry, DSME Geoje yard 2433)",
        url: "https://fleetphoto.ru/vessel/98086/",
      },
      {
        field: "Summer draft (exact Q88)",
        source: "не подтверждено — VesselFinder Draught blank; no public Q88 located for this hull",
        url: null,
      },
    ],
    flight_video: {
      ready: true,
      url: "/assets/arctic/videos/vessel_9750749.mp4",
      http_url: "/assets/arctic/videos/vessel_9750749.mp4",
      source_file: "georgiy-ushakov-ice-free-arctic1-flight.mp4",
      local_path: "C:\\111\\1001\\Artic\\georgiy-ushakov-ice-free-arctic1-flight.mp4",
      asset_source: "assets/arctic/videos/vessel_9750749_source.mp4",
      provider: "arctic_luma_flight",
      kind: "ai_generated_flight",
      provenance: "ai_generated",
      encoder_tag: "Lavf58.76.100",
      bytes: 1136848,
      fidelity_badge: ARCTIC_LUMA_VIDEO_BADGE,
    },
    video_derived: {
      badge: ARCTIC_VIDEO_DERIVED_BADGE,
      selection: "user_curated_replace_agent_ffmpeg",
      top_available: true,
      top_note: ARCTIC_TOP_VIEW_DERIVED_NOTE,
      side: {
        label: "SIDE-OBLIQUE (user-curated)",
        url: "/assets/arctic/frames/vessel_9750749_side_oblique.jpg",
        fallback_url: "assets/arctic/frames/vessel_9750749_side_oblique.jpg",
        user_source: "georgiy-ushakov-ice-free-arctic1-flight-2-1.png",
        note: "Elevated port three-quarter — NOT true side ortho",
      },
      bow: {
        label: "BOW (user-curated)",
        url: "/assets/arctic/frames/vessel_9750749_bow_oblique.jpg",
        fallback_url: "assets/arctic/frames/vessel_9750749_bow_oblique.jpg",
        user_source: "georgiy-ushakov-ice-free-arctic1-flight-2-2.jpg",
        note: "Near head-on bow — still from AI flight, not photograph",
      },
      top: {
        label: "TOP / OVERHEAD (user-curated)",
        url: "/assets/arctic/frames/vessel_9750749_top_overhead.jpg",
        fallback_url: "assets/arctic/frames/vessel_9750749_top_overhead.jpg",
        user_source: "georgiy-ushakov-ice-free-arctic1-flight-2-3.jpg",
        note: ARCTIC_TOP_VIEW_DERIVED_NOTE,
      },
    },
    refs: {
      side: {
        label: "VIDEO-DERIVED · SIDE-OBLIQUE",
        url: "/assets/arctic/frames/vessel_9750749_side_oblique.jpg",
        fallback_url: "assets/arctic/frames/vessel_9750749_side_oblique.jpg",
      },
    },
  },
];

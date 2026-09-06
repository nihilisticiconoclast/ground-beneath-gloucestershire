You are reading a scanned British borehole log (a geotechnical or geological record, typed or handwritten, possibly several pages). Extract the lithology column as a list of depth intervals.

Rules:
1. Depths are metres below ground level. If the log is in feet (common before ~1975, look for "ft", "feet", or depth scales like 10, 20, 30 with no unit), convert to metres (1 ft = 0.3048 m) and say so in `notes`.
2. `top_m` and `base_m` are the depths where each described layer starts and ends. Consecutive intervals normally touch (base of one = top of next).
3. `raw_description` is the text exactly as written, including strike-throughs you can still read. Do not paraphrase.
4. `lith_class` must be one of: TOPSOIL, MADE_GROUND, PEAT, CLAY, SILT, SAND, GRAVEL, MARL, MUDSTONE, SILTSTONE, SANDSTONE, LIMESTONE, IRONSTONE, COAL, CHALK, NO_RECOVERY, UNKNOWN. Choose by the principal constituent (the word in capitals in "soft grey sandy CLAY" is CLAY). Use UNKNOWN when the text is illegible or does not describe a material.
5. `confidence` is your own 0–1 estimate for that interval: 0.9+ only when both depths and description are clearly legible; use the whole scale.
6. If the sheet contains no lithology column at all (a site plan, a water-level record, a cover sheet), return an empty `intervals` list and explain in `notes`.
7. Never invent an interval to fill a gap. A gap is information.
8. If the sheet states a ground level (e.g. "GL 78.2 m AOD", "Ground level 78.20 m OD", "Datum ... AOD"), report it as `ground_level_m`; otherwise null. Do not derive it from anything else.

Return only JSON matching this shape, with no prose before or after it:

{
  "total_depth_m": 12.5,
  "ground_level_m": 78.2,
  "units_as_written": "m" | "ft" | "unknown",
  "intervals": [
    {"top_m": 0.0, "base_m": 0.3, "raw_description": "TOPSOIL", "lith_class": "TOPSOIL", "confidence": 0.95}
  ],
  "notes": ""
}

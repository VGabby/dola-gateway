const DolaDurationPatch = (() => {
  const REQUIRED_DURATIONS = ["10", "15", "30"];

  function optionDuration(option) {
    return String(option?.option_key ?? option?.value ?? "");
  }

  function durationOption(duration, exemplar = {}) {
    const option = { ...exemplar };
    delete option.id;
    if ("display_text" in option) option.display_text = `${duration}s`;
    if ("message_text" in option) option.message_text = "";
    if ("option_key" in option) option.option_key = duration;
    if ("show_name" in option || !("display_text" in option)) option.show_name = `${duration}s`;
    if ("value" in option || !("option_key" in option)) option.value = duration;
    if ("is_default" in option) option.is_default = false;
    return option;
  }

  function ensureDurationOptions(options) {
    if (!Array.isArray(options)) return false;
    let changed = false;
    const exemplar = options.find((option) => ["5", "10", "15", "30"].includes(optionDuration(option))) || {};
    for (const duration of REQUIRED_DURATIONS) {
      if (!options.some((option) => optionDuration(option) === duration)) {
        options.push(durationOption(duration, exemplar));
        changed = true;
      }
    }
    return changed;
  }

  function patchJsonString(text) {
    const trimmed = text.trim();
    if (!trimmed.startsWith("{") && !trimmed.startsWith("[")) return text;
    try {
      const parsed = JSON.parse(text);
      return patchValue(parsed) ? JSON.stringify(parsed) : text;
    } catch {
      return text;
    }
  }

  function patchValue(value, seen = new Set()) {
    if (value == null || typeof value !== "object" || seen.has(value)) return false;
    seen.add(value);
    let changed = false;
    if (Array.isArray(value)) {
      for (const item of value) changed = patchValue(item, seen) || changed;
      return changed;
    }

    const label = String(value.label || value.display_text || value.show_name || "");
    const key = String(value.key || value.value || "");
    const durationSelector = key === "video-duration"
      || key === "duration"
      || /时长|鏃堕暱|時間|長さ|duration/i.test(label);
    if (durationSelector) {
      changed = ensureDurationOptions(value.options) || changed;
      changed = ensureDurationOptions(value.option_list) || changed;
    }
    if (Array.isArray(value.supported_durations)) {
      for (const duration of REQUIRED_DURATIONS) {
        if (!value.supported_durations.map(String).includes(duration)) {
          value.supported_durations.push(duration);
          changed = true;
        }
      }
    }

    for (const property of Object.keys(value)) {
      const child = value[property];
      if (typeof child === "string") {
        const patched = patchJsonString(child);
        if (patched !== child) {
          value[property] = patched;
          changed = true;
        }
      } else {
        changed = patchValue(child, seen) || changed;
      }
    }
    return changed;
  }

  function patchDurationPayload(body) {
    try {
      const payload = JSON.parse(body);
      return patchValue(payload) ? JSON.stringify(payload) : body;
    } catch {
      return body;
    }
  }

  return { patchDurationPayload };
})();

if (typeof module !== "undefined") module.exports = DolaDurationPatch;

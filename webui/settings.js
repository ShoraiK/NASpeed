// SPDX-FileCopyrightText: 2026 NASpeed contributors
// SPDX-License-Identifier: GPL-3.0-only
/* NASpeed - radio groups, settings panel and about metadata. */
(() => {
  "use strict";

  const ARROW_STEPS = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 };

  function createSettings(deps) {
    const { state, $, el, externalLink, t, has, fmtInt } = deps;
    const segConfigs = new WeakMap();
    let flashTimer = 0;

    function flash(text) {
      $("set-status").textContent = text;
      window.clearTimeout(flashTimer);
      flashTimer = window.setTimeout(() => {
        $("set-status").textContent = "";
      }, 2500);
    }

    function bindRadioGroupKeys(container, options) {
      container.addEventListener("keydown", (event) => {
        const step = ARROW_STEPS[event.key];
        if (!step || (options.canMove && !options.canMove())) {
          return;
        }
        const radios = Array.from(container.querySelectorAll("button[role='radio']"));
        if (!radios.length) {
          return;
        }
        event.preventDefault();
        let index = options.currentIndex ? options.currentIndex(radios, event) : radios.indexOf(event.target);
        if (index < 0) {
          index = 0;
        }
        const next = radios[(index + step + radios.length) % radios.length];
        if (options.onMove) {
          options.onMove(next, radios);
        }
        let target = next;
        if (next.isConnected === false && options.findReplacement) {
          target = options.findReplacement(Array.from(container.querySelectorAll("button[role='radio']")), next) || next;
        }
        if (target && target.focus) {
          target.focus();
        }
      });
    }

    function buildSeg(id, options, current, onPick) {
      const box = $(id);
      box.textContent = "";
      segConfigs.set(box, { options, onPick });
      if (!box.dataset.segBound) {
        box.dataset.segBound = "1";
        bindRadioGroupKeys(box, {
          onMove: (next) => {
            const active = segConfigs.get(box);
            const picked = active && active.options[Number(next.dataset.index)];
            if (picked) {
              active.onPick(picked.value);
            }
          },
          findReplacement: (radios, previous) => {
            return radios.find((radio) => radio.dataset.value === previous.dataset.value);
          }
        });
      }
      options.forEach((option, index) => {
        const button = el("button", "", option.label);
        const selected = String(option.value) === String(current);
        button.type = "button";
        button.setAttribute("role", "radio");
        button.setAttribute("aria-checked", String(selected));
        button.tabIndex = selected ? 0 : -1;
        button.dataset.value = String(option.value);
        button.dataset.index = String(index);
        button.addEventListener("click", () => {
          onPick(option.value);
        });
        box.append(button);
      });
    }

    // Moves the choice of a group built by buildSeg without building it again.
    function markSeg(id, value) {
      const buttons = $(id).querySelectorAll("button[role='radio']");
      for (let i = 0; i < buttons.length; i += 1) {
        const on = buttons[i].dataset.value === String(value);
        buttons[i].setAttribute("aria-checked", String(on));
        buttons[i].tabIndex = on ? 0 : -1;
      }
    }

    function syncSettings() {
      if (!state.info) {
        return;
      }
      const settings = state.settings;
      const options = state.info.options || {};
      markSeg("set-duration", settings.duration);
      markSeg("set-connections", String(settings.connections));
      markSeg("set-ip", String(settings.ip_version));
      const saveOn = settings.save_history !== false;
      $("set-history").setAttribute("aria-checked", String(saveOn));
      $("set-history-state").textContent = t(saveOn ? "value.on" : "value.off");
      const noticeButton = $("set-notice");
      const noticeName = noticeButton.getAttribute("data-notice");
      if (noticeName) {
        noticeButton.setAttribute("aria-checked", String(!(settings.hidden_notices || {})[noticeName]));
      }
      const limit = $("set-limit");
      limit.max = String(options.history_max || 1000);
      limit.value = String(settings.history_limit);
    }

    function saveSetting(patch) {
      return deps.api("POST", "/settings", patch).then(
        (settings) => {
          state.settings = settings;
          flash(t("set.saved"));
          syncSettings();
          if (state.view === "test") {
            deps.renderTest();
          }
          return true;
        },
        () => {
          if (!state.gate) {
            flash(t("set.failed"));
            syncSettings();
          }
          return false;
        }
      );
    }

    function buildSettings() {
      if (!state.info) {
        return;
      }
      const settings = state.settings;
      const options = state.info.options || {};
      const durations = options.durations || {};
      buildSeg(
        "set-duration",
        ["quick", "standard", "long"]
          .filter((key) => durations[key] !== undefined)
          .map((key) => ({ value: key, label: t("duration." + key) + " · " + durations[key] + " s" })),
        settings.duration,
        (value) => { saveSetting({ duration: value }); }
      );
      buildSeg(
        "set-connections",
        (options.connections || []).map((value) => ({
          value,
          label: value === "auto" ? t("value.auto") : value
        })),
        String(settings.connections),
        (value) => { saveSetting({ connections: value }); }
      );
      buildSeg(
        "set-ip",
        (options.ip_versions || []).map((value) => ({
          value,
          label: value === "auto" ? t("value.auto") : "IPv" + value
        })),
        String(settings.ip_version),
        (value) => { saveSetting({ ip_version: value }); }
      );
      // Only M-Lab has a notice today; the switch follows the first provider that has one.
      const noticed = state.providers.filter((provider) => provider.notice)[0];
      $("notice-row").hidden = !noticed;
      if (noticed) {
        $("set-notice").setAttribute("data-notice", noticed.notice);
      }
      syncSettings();
      $("set-limit-help").textContent = t("set.limit.help", { max: fmtInt(options.history_max || 1000) });

      const version = state.info.version;
      $("about-version").textContent = "NASpeed " + (state.info.beta ? t("about.beta", { version }) : version);
      $("about-mode").textContent = has("about.mode." + state.info.mode) ? t("about.mode." + state.info.mode) : "";
      const licenseParts = t("about.license").split("{license}");
      const licenseLink = externalLink("GPL-3.0-only", "https://www.gnu.org/licenses/gpl-3.0.html");
      $("about-license").textContent = "";
      $("about-license").append(licenseParts[0], licenseLink, licenseParts.slice(1).join(""));
      const list = $("about-providers");
      list.textContent = "";
      state.providers.forEach((provider) => {
        const name = el("dt");
        if (/^https:\/\//.test(provider.homepage || "")) {
          name.append(externalLink(provider.name, provider.homepage));
        } else {
          name.textContent = provider.name;
        }
        const item = el("div", "s-prov");
        item.append(name, el("dd", "", has("about.prov." + provider.id) ? t("about.prov." + provider.id) : ""));
        list.append(item);
      });
    }

    return { flash, bindRadioGroupKeys, buildSeg, markSeg, syncSettings, saveSetting, buildSettings };
  }

  window.NASpeedSettings = { createSettings };
})();

(function () {
  var BUTTON_STATE_MS = 1000;

  async function copyToClipboard(text) {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return;
    }

    var textarea = document.createElement("textarea");
    textarea.value = text;
    textarea.setAttribute("readonly", "");
    textarea.style.position = "absolute";
    textarea.style.left = "-9999px";
    document.body.appendChild(textarea);
    textarea.select();
    document.execCommand("copy");
    document.body.removeChild(textarea);
  }

  function getLabelNode(btn) {
    return btn.querySelector("[data-copy-label]");
  }

  function setCopyButtonState(btn, state, label) {
    btn.classList.remove("btn-copy-success", "btn-copy-error");
    if (state === "success") {
      btn.classList.add("btn-copy-success");
    } else if (state === "error") {
      btn.classList.add("btn-copy-error");
    }

    var labelNode = getLabelNode(btn);
    if (labelNode) {
      labelNode.textContent = label;
      return;
    }

    btn.textContent = label;
  }

  function getAddLabelNode(btn) {
    return btn.querySelector("[data-add-label]");
  }

  function setAddButtonState(btn, state, label) {
    btn.classList.remove("btn-add-active");
    if (state === "active") {
      btn.classList.add("btn-add-active");
    }

    var labelNode = getAddLabelNode(btn);
    if (labelNode) {
      labelNode.textContent = label;
      return;
    }

    btn.textContent = label;
  }

  function updateDeviceUsageCounters(activeDevices, maxDevices) {
    var counter = document.querySelector("[data-devices-counter]");
    var summary = document.querySelector("[data-devices-summary]");
    var ring = document.querySelector("[data-device-ring]");
    if (!counter || !summary || !ring) return;

    var active = Number(activeDevices);
    var max = Number(maxDevices);
    if (!Number.isFinite(active) || active < 0) active = 0;
    if (!Number.isFinite(max) || max < 0) max = 0;

    counter.textContent = active + "/" + max;
    summary.textContent = "УСТРОЙСТВА " + active + "/" + max;

    var ratio = max > 0 ? active / max : 0;
    if (ratio < 0) ratio = 0;
    if (ratio > 1) ratio = 1;
    var ringOffset = 402 - 402 * ratio;
    ring.style.setProperty("--ring-offset", ringOffset.toFixed(2));
  }

  function ensureDevicesEmptyState() {
    var list = document.querySelector("[data-devices-list]");
    if (!list) return;
    if (list.querySelector("[data-device-row]")) return;

    var section = document.querySelector("[data-devices-section]");
    if (!section) return;

    section.classList.add("devices-section-empty");
    if (section.tagName === "DETAILS") {
      section.setAttribute("open", "");
    }

    if (!section.querySelector("[data-device-empty]")) {
      var content = section.querySelector(".devices-content");
      if (!content) return;
      var empty = document.createElement("div");
      empty.className = "device-empty";
      empty.setAttribute("data-device-empty", "true");
      empty.textContent = "Пока нет подключенных устройств";
      content.appendChild(empty);
    }
  }

  function setupCopySubscriptionButton() {
    var btn = document.querySelector("[data-action-copy-subscription]");
    if (!btn) return;

    var defaultLabel = btn.getAttribute("data-default-label") || "Скопировать ссылку";
    var successLabel = btn.getAttribute("data-success-label") || "Ссылка скопирована";
    var errorLabel = btn.getAttribute("data-error-label") || "Не удалось скопировать";

    btn.addEventListener("click", async function () {
      var value = btn.getAttribute("data-subscription-url") || "";
      if (!value) {
        setCopyButtonState(btn, "error", errorLabel);
        if (btn.__copyStateTimer) {
          window.clearTimeout(btn.__copyStateTimer);
        }
        btn.__copyStateTimer = window.setTimeout(function () {
          setCopyButtonState(btn, "default", defaultLabel);
          btn.__copyStateTimer = null;
        }, BUTTON_STATE_MS);
        return;
      }

      if (btn.__copyStateTimer) {
        window.clearTimeout(btn.__copyStateTimer);
        btn.__copyStateTimer = null;
      }

      try {
        await copyToClipboard(value);
        setCopyButtonState(btn, "success", successLabel);
      } catch (err) {
        setCopyButtonState(btn, "error", errorLabel);
      }

      btn.__copyStateTimer = window.setTimeout(function () {
        setCopyButtonState(btn, "default", defaultLabel);
        btn.__copyStateTimer = null;
      }, BUTTON_STATE_MS);
    });
  }

  function setupAddSubscriptionButton() {
    var btn = document.querySelector("[data-action-add-subscription]");
    if (!btn) return;

    var defaultLabel = btn.getAttribute("data-default-label") || "Добавить в подписку";
    var loadingLabel = btn.getAttribute("data-loading-label") || "Открываем Happ...";

    btn.addEventListener("click", function () {
      var happLink = btn.getAttribute("data-happ-link");
      var subscriptionUrl = btn.getAttribute("data-subscription-url");
      if (btn.__addStateTimer) {
        window.clearTimeout(btn.__addStateTimer);
        btn.__addStateTimer = null;
      }
      if (btn.__addFallbackTimer) {
        window.clearTimeout(btn.__addFallbackTimer);
        btn.__addFallbackTimer = null;
      }

      if (!happLink || !subscriptionUrl) {
        setAddButtonState(btn, "default", defaultLabel);
        return;
      }

      setAddButtonState(btn, "active", loadingLabel);

      btn.__addStateTimer = window.setTimeout(function () {
        setAddButtonState(btn, "default", defaultLabel);
        btn.__addStateTimer = null;
      }, BUTTON_STATE_MS);

      // Navigate directly to the happ:// deep link from the user's click event.
      // This triggers the "Open Happ?" system prompt inline — no new tab, no
      // about:blank page. On iOS and Android, direct navigation from a click
      // handler is treated as a user gesture and is not blocked by the browser.
      window.location.href = happLink;

      // If the app did not open (page is still visible after the system prompt
      // timeout), fall back to the raw subscription URL so the user can copy it.
      btn.__addFallbackTimer = window.setTimeout(function () {
        if (document.visibilityState !== "hidden") {
          window.location.href = subscriptionUrl;
        }
        btn.__addFallbackTimer = null;
      }, 1500);
    });
  }

  function setupRemoveDeviceButtons() {
    var buttons = document.querySelectorAll("[data-action-remove-device]");
    if (!buttons.length) return;

    var dashboard = document.querySelector("[data-user-token]");
    if (!dashboard) return;
    var token = dashboard.getAttribute("data-user-token") || "";
    var maxDevices = Number(dashboard.getAttribute("data-max-devices") || "0");

    buttons.forEach(function (btn) {
      btn.addEventListener("click", async function () {
        var deviceId = btn.getAttribute("data-device-id") || "";
        if (!token || !deviceId) {
          btn.classList.add("is-error");
          window.setTimeout(function () {
            btn.classList.remove("is-error");
          }, 1400);
          return;
        }

        btn.disabled = true;
        btn.classList.remove("is-error");
        btn.classList.add("is-busy");

        try {
          var response = await fetch("/api/device/remove", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
            },
            body: JSON.stringify({
              token: token,
              device_id: deviceId,
            }),
          });

          if (!response.ok) {
            throw new Error("remove_failed");
          }

          var payload = await response.json();
          var row = btn.closest("[data-device-row]");
          if (row) {
            row.remove();
          }

          var activeDevices = Number(payload.active_devices);
          if (!Number.isFinite(activeDevices)) {
            activeDevices = 0;
          }
          updateDeviceUsageCounters(activeDevices, payload.max_devices || maxDevices);
          ensureDevicesEmptyState();
        } catch (err) {
          btn.classList.add("is-error");
          btn.disabled = false;
          btn.classList.remove("is-busy");
          window.setTimeout(function () {
            btn.classList.remove("is-error");
          }, 1800);
          return;
        }

        btn.disabled = false;
        btn.classList.remove("is-busy");
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    setupCopySubscriptionButton();
    setupAddSubscriptionButton();
    setupRemoveDeviceButtons();
  });
})();

(function () {
  var STORAGE_KEY = "shashkoffvpn_device_id_v1";

  function generateDeviceId() {
    var ts = Date.now().toString(36);
    var rand = Math.random().toString(36).slice(2, 10);
    return "web-" + ts + "-" + rand;
  }

  function getOrCreateDeviceId() {
    var existing = window.localStorage.getItem(STORAGE_KEY);
    if (existing && existing.trim()) {
      return existing;
    }

    var created = generateDeviceId();
    window.localStorage.setItem(STORAGE_KEY, created);
    return created;
  }

  function buildHref(baseUrl, deviceId, mode) {
    var href = baseUrl + "?device_id=" + encodeURIComponent(deviceId);
    if (mode) {
      href += "&mode=" + encodeURIComponent(mode);
    }
    return href;
  }

  function attachDeviceIdToOpenLinks() {
    var links = document.querySelectorAll("[data-open-in-happ]");
    if (!links.length) {
      return;
    }

    var deviceId = getOrCreateDeviceId();
    links.forEach(function (link) {
      var baseUrl = link.getAttribute("data-open-base");
      if (!baseUrl) {
        return;
      }

      var mode = link.getAttribute("data-open-mode");
      link.href = buildHref(baseUrl, deviceId, mode);
    });
  }

  document.addEventListener("DOMContentLoaded", attachDeviceIdToOpenLinks);
})();

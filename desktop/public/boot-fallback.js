(function () {
  "use strict";

  var bootError = "";
  function rememberError(message) {
    bootError = String(message || "未知脚本错误");
  }
  window.addEventListener("error", function (event) {
    rememberError(event && event.message);
  });
  window.addEventListener("unhandledrejection", function (event) {
    var reason = event && event.reason;
    rememberError(reason && reason.message ? reason.message : reason);
  });
  window.setTimeout(function () {
    if (window.__ECHODESK_REACT_MOUNTED__) return;
    var root = document.getElementById("echodesk-boot-fallback");
    if (!root) return;
    while (root.firstChild) root.removeChild(root.firstChild);

    var heading = document.createElement("div");
    heading.style.fontSize = "24px";
    heading.style.fontWeight = "700";
    heading.style.marginBottom = "8px";
    heading.style.color = "#b91c1c";
    heading.textContent = "EchoDesk 启动失败";
    root.appendChild(heading);

    var detail = document.createElement("div");
    detail.style.fontSize = "16px";
    detail.style.color = "#444";
    detail.textContent = bootError || "应用在 12 秒内没有完成启动";
    root.appendChild(detail);
  }, 12000);
})();

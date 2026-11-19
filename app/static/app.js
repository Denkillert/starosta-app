(function () {
  "use strict";

  // --- автоскрытие уведомлений ------------------------------------------
  document.querySelectorAll(".flash").forEach(function (el) {
    window.setTimeout(function () {
      el.classList.add("is-hidden");
    }, 6500);
  });

  // --- тост --------------------------------------------------------------
  var toastTimer = null;
  function showToast(text) {
    var el = document.querySelector(".toast");
    if (!el) {
      el = document.createElement("div");
      el.className = "toast";
      document.body.appendChild(el);
    }
    el.textContent = text;
    el.classList.add("show");
    window.clearTimeout(toastTimer);
    toastTimer = window.setTimeout(function () {
      el.classList.remove("show");
    }, 2200);
  }

  // --- копирование кода приглашения -------------------------------------
  document.querySelectorAll("[data-copy]").forEach(function (button) {
    button.addEventListener("click", function () {
      var value = button.getAttribute("data-copy") || "";
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(value).then(
          function () {
            showToast("Скопировано: " + value);
          },
          function () {
            window.prompt("Скопируйте код:", value);
          }
        );
      } else {
        window.prompt("Скопируйте код:", value);
      }
    });
  });

  // --- подтверждение опасных действий -----------------------------------
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (event) {
      if (!window.confirm(form.getAttribute("data-confirm"))) {
        event.preventDefault();
      }
    });
  });

  // --- скачивание файлов --------------------------------------------------
  // Внутри Telegram WebView файлы не сохраняются: ссылку нужно отдать
  // системному браузеру через WebApp.openLink.
  document.querySelectorAll("a[data-external]").forEach(function (link) {
    link.addEventListener("click", function (event) {
      var telegram = window.Telegram && window.Telegram.WebApp;
      if (!telegram || typeof telegram.openLink !== "function") return;
      if (telegram.platform === "web") return;
      event.preventDefault();
      telegram.openLink(link.href);
    });
  });

  // --- защита от двойной отправки ---------------------------------------
  document.querySelectorAll("form").forEach(function (form) {
    form.addEventListener("submit", function () {
      var button = form.querySelector('button[type="submit"]');
      if (!button) return;
      window.setTimeout(function () {
        if (button.disabled) return;
        button.disabled = true;
        window.setTimeout(function () {
          button.disabled = false;
        }, 6000);
      }, 0);
    });
  });
})();

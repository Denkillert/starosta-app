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

  // --- цвет select по статусу -------------------------------------------
  var STATUS_CLASSES = [
    "status-present",
    "status-late",
    "status-absent",
    "status-excused",
    "status-not_answered",
  ];

  function paint(select) {
    STATUS_CLASSES.forEach(function (name) {
      select.classList.remove(name);
    });
    select.classList.add("status-" + select.value);
  }

  document.addEventListener("change", function (event) {
    if (event.target && event.target.classList.contains("status-select")) {
      paint(event.target);
    }
  });

  // --- «Все были» --------------------------------------------------------
  document.querySelectorAll("[data-all-present]").forEach(function (button) {
    button.addEventListener("click", function () {
      var form = button.closest("form");
      if (!form) return;
      form.querySelectorAll(".status-select").forEach(function (select) {
        select.value = "present";
        paint(select);
      });
      showToast("Отмечено «Был». Не забудьте сохранить.");
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

  // --- условные поля формы документа ------------------------------------
  document.querySelectorAll("[data-doc-form]").forEach(function (form) {
    var type = form.getAttribute("data-doc-type");
    form.querySelectorAll("[data-show-for]").forEach(function (block) {
      var allowed = (block.getAttribute("data-show-for") || "").split(/\s+/);
      block.hidden = allowed.indexOf(type) === -1;
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

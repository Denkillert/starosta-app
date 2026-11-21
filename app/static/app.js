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
  // --- журнал: локальные отметки и одно подтверждение ---------------------
  (function () {
    var bar = document.querySelector("[data-att-commit]");
    var saveBtn = bar && bar.querySelector("[data-att-save]");
    var discardBtn = bar && bar.querySelector("[data-att-discard]");
    var label = bar && bar.querySelector("[data-att-commit-label]");
    var table = document.querySelector(".att-table");
    if (!bar || !saveBtn || !discardBtn || !label || !table) return;

    var groupId = bar.getAttribute("data-group") || "";
    var cells = Array.prototype.slice.call(
      table.querySelectorAll("[data-att-cell]")
    );
    var pending = {};
    var saving = false;

    function keyOf(cell) {
      return cell.getAttribute("data-occurrence") + ":" + cell.getAttribute("data-member");
    }

    function statusOf(cell) {
      return cell.getAttribute("data-status") || "present";
    }

    function closestWithAttr(node, attr, stop) {
      while (node && node !== stop) {
        if (node.nodeType === 1 && node.hasAttribute(attr)) return node;
        node = node.parentNode;
      }
      return null;
    }

    function setCell(cell, status) {
      cell.setAttribute("data-status", status);
      cell.className = "att-cell att-" + status;
      var button = cell.querySelector("[data-att-toggle]");
      if (!button) return;
      var absent = status === "absent";
      button.className = "att-btn att-btn-" + status;
      button.textContent = absent ? "Н" : "✓";
      button.setAttribute("aria-pressed", absent ? "true" : "false");
      var title = button.getAttribute("title") || "";
      var parts = title.split(" · ");
      button.setAttribute(
        "title",
        (absent ? "Вернуть «присутствует»" : "Отметить пропуск") +
          (parts.length > 1 ? " · " + parts.slice(1).join(" · ") : "")
      );
    }

    function refreshCell(cell) {
      var dirty = statusOf(cell) !== cell.getAttribute("data-original");
      if (dirty) {
        cell.classList.add("is-dirty");
        pending[keyOf(cell)] = {
          occurrence_id: Number(cell.getAttribute("data-occurrence")),
          member_id: Number(cell.getAttribute("data-member")),
          status: statusOf(cell),
        };
      } else {
        cell.classList.remove("is-dirty");
        delete pending[keyOf(cell)];
      }
    }

    function refreshCounts(occurrenceId) {
      var header = table.querySelector(
        '[data-att-count="' + occurrenceId + '"]'
      );
      if (!header) return;
      var absent = 0;
      var excused = 0;
      var total = 0;
      cells.forEach(function (cell) {
        if (cell.getAttribute("data-occurrence") !== String(occurrenceId)) return;
        total += 1;
        var status = statusOf(cell);
        if (status === "absent") absent += 1;
        else if (status === "excused") excused += 1;
      });
      var present = Math.max(0, total - absent - excused);
      header.querySelector("[data-att-present]").textContent = String(present);
      header.querySelector("[data-att-total]").textContent = String(total);
      if (absent + excused > 0) header.classList.add("is-partial");
      else header.classList.remove("is-partial");
    }

    function pendingCount() {
      var count = 0;
      for (var key in pending) {
        if (Object.prototype.hasOwnProperty.call(pending, key)) count += 1;
      }
      return count;
    }

    function plural(n) {
      var mod10 = n % 10;
      var mod100 = n % 100;
      if (mod10 === 1 && mod100 !== 11) return "отметка";
      if (mod10 >= 2 && mod10 <= 4 && (mod100 < 12 || mod100 > 14)) return "отметки";
      return "отметок";
    }

    function refreshBar() {
      var count = pendingCount();
      document.body.classList.toggle("has-att-commit", count > 0);
      if (!count) {
        bar.hidden = true;
        return;
      }
      bar.hidden = false;
      label.textContent = "Не сохранено: " + count + " " + plural(count);
    }

    table.addEventListener("click", function (event) {
      if (saving) return;
      var button = closestWithAttr(event.target, "data-att-toggle", table);
      if (!button) return;
      var cell = closestWithAttr(button, "data-att-cell", table);
      if (!cell || cell.hasAttribute("data-frozen")) return;

      var next = statusOf(cell) === "absent" ? "present" : "absent";
      setCell(cell, next);
      refreshCell(cell);
      refreshCounts(cell.getAttribute("data-occurrence"));
      refreshBar();
    });

    discardBtn.addEventListener("click", function () {
      if (saving) return;
      var touched = {};
      cells.forEach(function (cell) {
        if (cell.hasAttribute("data-frozen")) return;
        var original = cell.getAttribute("data-original");
        if (statusOf(cell) !== original) setCell(cell, original);
        cell.classList.remove("is-dirty");
        touched[cell.getAttribute("data-occurrence")] = true;
      });
      pending = {};
      for (var occurrenceId in touched) {
        if (Object.prototype.hasOwnProperty.call(touched, occurrenceId)) {
          refreshCounts(occurrenceId);
        }
      }
      refreshBar();
    });

    saveBtn.addEventListener("click", function () {
      if (saving) return;
      var changes = [];
      for (var key in pending) {
        if (Object.prototype.hasOwnProperty.call(pending, key)) {
          changes.push(pending[key]);
        }
      }
      if (!changes.length) return;

      saving = true;
      saveBtn.disabled = true;
      saveBtn.textContent = "Сохраняю…";

      fetch("/actions/attendance/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        credentials: "same-origin",
        body: JSON.stringify({ group: groupId, changes: changes }),
      })
        .then(function (response) {
          return response
            .json()
            .catch(function () {
              return { ok: false, error: "Сервер вернул неожиданный ответ" };
            })
            .then(function (data) {
              return { response: response, data: data };
            });
        })
        .then(function (result) {
          if (!result.response.ok || !result.data.ok) {
            throw new Error(result.data.error || "Не удалось сохранить отметки");
          }
          cells.forEach(function (cell) {
            if (Object.prototype.hasOwnProperty.call(pending, keyOf(cell))) {
              cell.setAttribute("data-original", statusOf(cell));
              cell.classList.remove("is-dirty");
            }
          });
          pending = {};
          refreshBar();
          showToast("Отметки сохранены");
          var tg = window.Telegram && window.Telegram.WebApp;
          if (tg && tg.HapticFeedback && tg.HapticFeedback.notificationOccurred) {
            tg.HapticFeedback.notificationOccurred("success");
          }
        })
        .catch(function (error) {
          showToast(error.message || "Нет связи с сервером");
        })
        .then(function () {
          saving = false;
          saveBtn.disabled = false;
          saveBtn.textContent = "Подтвердить";
        });
    });

    window.addEventListener("beforeunload", function (event) {
      if (!pendingCount()) return;
      event.preventDefault();
      event.returnValue = "";
    });
  })();
})();

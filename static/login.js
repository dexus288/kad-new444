"use strict";
(() => {
  const $ = (s) => document.querySelector(s);
  const msg = $("#msg");
  const show = (text, ok) => { msg.textContent = text; msg.className = "msg" + (ok ? " ok" : ""); msg.hidden = !text; };

  const next = (() => {
    const n = new URLSearchParams(location.search).get("next") || "/";
    return n.startsWith("/") && !n.startsWith("//") ? n : "/";
  })();

  function tab(name) {
    document.querySelectorAll("[data-tab]").forEach((b) => b.classList.toggle("on", b.dataset.tab === name));
    $("#loginForm").hidden = name !== "login";
    $("#registerForm").hidden = name !== "register";
    show("");
  }
  document.querySelectorAll("[data-tab]").forEach((b) => b.addEventListener("click", () => tab(b.dataset.tab)));

  fetch("/api/config").then((r) => r.json()).then((c) => {
    if (c.registration === "closed") $(".seg").hidden = true;
    if (c.registration === "invite") {
      $("#inviteField").hidden = false;
      $("#inviteField input").required = true;
      $("#regHint").textContent = "Код запрошення видає адміністратор.";
    }
  }).catch(() => {});

  async function post(url, data) {
    const r = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "X-Requested-With": "kd" },
      body: JSON.stringify(data),
    });
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof j.detail === "string" ? j.detail : "Помилка " + r.status);
    return j;
  }

  function bind(form, url, onOk) {
    form.addEventListener("submit", async (e) => {
      e.preventDefault();
      const btn = form.querySelector("button[type=submit]");
      btn.disabled = true;
      try {
        onOk(await post(url, Object.fromEntries(new FormData(form))));
      } catch (err) {
        show(err.message);
      } finally {
        btn.disabled = false;
      }
    });
  }

  bind($("#loginForm"), "/api/auth/login", () => location.replace(next));
  bind($("#registerForm"), "/api/auth/register", (j) => {
    if (j.status === "active") return location.replace(next);
    tab("login");
    show("Заявку надіслано. Увійти можна після підтвердження адміністратором.", true);
  });
})();

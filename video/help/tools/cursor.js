// A mouse pointer for the recording. Headless Chromium draws none, so this
// page script draws one where the mouse is, and a ring where it clicks. It
// follows real mouse events (the ones Playwright's page.mouse sends), so it
// is never somewhere the mouse is not. Injected into every page recorded.
(() => {
  if (window.__helpCursor) return;
  window.__helpCursor = true;
  const SVG =
    '<svg xmlns="http://www.w3.org/2000/svg" width="26" height="26" viewBox="0 0 26 26">' +
    '<path d="M3 2 L3 21 L8 16.5 L11.5 24 L15 22.5 L11.5 15 L18 15 Z" fill="#fff" stroke="#111" stroke-width="1.6" stroke-linejoin="round"/></svg>';
  function mount() {
    const root = document.createElement("div");
    root.setAttribute("aria-hidden", "true");
    root.style.cssText = "position:fixed;inset:0;pointer-events:none;z-index:2147483647;";
    const arrow = document.createElement("div");
    arrow.innerHTML = SVG;
    arrow.style.cssText =
      "position:absolute;left:0;top:0;width:26px;height:26px;transform:translate(-100px,-100px);" +
      "filter:drop-shadow(0 2px 3px rgba(0,0,0,.35));transition:opacity .2s;";
    root.appendChild(arrow);
    document.documentElement.appendChild(root);
    let x = -100, y = -100;
    const place = () => (arrow.style.transform = `translate(${x - 3}px, ${y - 2}px)`);
    addEventListener("mousemove", (e) => { x = e.clientX; y = e.clientY; place(); }, true);
    addEventListener("mousedown", (e) => {
      const ring = document.createElement("div");
      ring.style.cssText =
        `position:absolute;left:${e.clientX - 18}px;top:${e.clientY - 18}px;width:36px;height:36px;border-radius:50%;` +
        "border:3px solid rgba(66,133,244,.85);background:rgba(66,133,244,.18);transform:scale(.4);opacity:1;" +
        "transition:transform .45s ease-out, opacity .45s ease-out;";
      root.insertBefore(ring, arrow);
      requestAnimationFrame(() => requestAnimationFrame(() => { ring.style.transform = "scale(1.4)"; ring.style.opacity = "0"; }));
      setTimeout(() => ring.remove(), 600);
    }, true);
    // The recorder hides the pointer while it types, as a person's hand leaves the mouse.
    window.__helpCursorShow = (on) => (arrow.style.opacity = on ? "1" : "0");
    // A keyboard shortcut, shown as keycaps at the bottom while it is pressed: the viewer can't see the keyboard.
    window.__helpKeys = (label) => {
      const caps = document.createElement("div");
      caps.style.cssText =
        "position:absolute;left:50%;bottom:28px;transform:translateX(-50%);display:flex;gap:6px;" +
        "transition:opacity .3s;opacity:1;";
      for (const key of label.split("+")) {
        const cap = document.createElement("span");
        cap.textContent = key;
        cap.style.cssText =
          "padding:6px 12px;border-radius:7px;font:600 18px system-ui,sans-serif;color:#fff;" +
          "background:rgba(15,19,28,.88);box-shadow:0 2px 0 rgba(255,255,255,.18) inset,0 4px 14px rgba(0,0,0,.3);";
        caps.appendChild(cap);
      }
      root.appendChild(caps);
      setTimeout(() => (caps.style.opacity = "0"), 1100);
      setTimeout(() => caps.remove(), 1500);
    };
  }
  if (document.documentElement) mount();
  else document.addEventListener("DOMContentLoaded", mount);
})();

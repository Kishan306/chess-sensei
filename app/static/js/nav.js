// Top bar: the Masterclass switch, shared by every page.
const listeners = [];
let state = { masterclass: false };

export function isMasterclass() {
  return state.masterclass;
}

/** Call fn(on) whenever the switch changes. */
export function onMasterclassChange(fn) {
  listeners.push(fn);
}

function render() {
  const nav = document.querySelector("nav.menu");
  if (!nav || nav.querySelector(".mc-switch")) return;
  const input = document.createElement("input");
  input.type = "checkbox";
  input.checked = state.masterclass;
  input.setAttribute("role", "switch");
  input.addEventListener("change", async () => {
    const wanted = input.checked;
    try {
      const res = await fetch("/api/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ masterclass: wanted }),
      });
      if (!res.ok) throw new Error();
      state = await res.json();
      document.body.classList.toggle("masterclass", state.masterclass);
      listeners.forEach((fn) => fn(state.masterclass));
    } catch {
      input.checked = !wanted;
      alert("Couldn't change Masterclass mode.");
    }
  });
  const label = document.createElement("label");
  label.className = "mc-switch";
  label.title = "Masterclass: every move explained in depth by a titled-player-level coach. Uses many more LLM calls.";
  const track = document.createElement("span");
  track.className = "track";
  label.append(input, track, document.createTextNode("Masterclass"));
  nav.append(label);
}

/** Resolves once the current setting has been loaded. */
export const ready = (async () => {
  try {
    const res = await fetch("/api/settings");
    if (res.ok) state = await res.json();
  } catch {
    /* keep the default */
  }
  document.body.classList.toggle("masterclass", state.masterclass);
  render();
  return state;
})();

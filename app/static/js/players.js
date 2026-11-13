// Shared helpers + the "which player are you?" control used on the home and review pages.

export function el(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (v === null || v === undefined) continue;
    if (k === "class") node.className = v;
    else if (k.startsWith("on")) node.addEventListener(k.slice(2), v);
    else node.setAttribute(k, v);
  }
  for (const c of children) node.append(c instanceof Node ? c : document.createTextNode(c ?? ""));
  return node;
}

export async function saveMyColor(gameId, color) {
  const res = await fetch(`/api/games/${gameId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ my_color: color }),
  });
  if (!res.ok) throw new Error("Couldn't save your colour");
}

/**
 * Two buttons, one per player. Clicking a name marks that player as you;
 * clicking your own name again clears it. onChange(color) runs after a successful save.
 */
export function playerPicker(game, onChange = () => {}) {
  const box = el("div", { class: "players", role: "group", "aria-label": "Which player are you?" });

  const render = () => {
    box.replaceChildren(...["white", "black"].map((color) => {
      const name = game[color] || (color === "white" ? "White" : "Black");
      const elo = game[`${color}_elo`];
      const isMe = game.my_color === color;
      return el("button", {
        type: "button",
        class: `player${isMe ? " me" : ""}`,
        "aria-pressed": String(isMe),
        title: isMe ? "This is you — click to unset" : "Click if this is you",
        onclick: () => choose(isMe ? null : color),
      },
        el("span", { class: "piece" }, color === "white" ? "♔" : "♚"),
        el("span", { class: "name" }, name),
        elo ? el("span", { class: "elo" }, String(elo)) : "",
        isMe ? el("span", { class: "you" }, "You") : "");
    }));
    box.classList.toggle("unset", !game.my_color);
  };

  const choose = async (color) => {
    const previous = game.my_color;
    game.my_color = color; // update straight away, undo if the save fails
    render();
    try {
      await saveMyColor(game.id, color);
      onChange(color);
    } catch (err) {
      game.my_color = previous;
      render();
      alert(err.message);
    }
  };

  render();
  return box;
}

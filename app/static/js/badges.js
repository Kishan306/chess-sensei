// Move-quality badges: a coloured circle with a white symbol, like chess.com.
// Used on the board (top-right corner of the moved piece) and in the move list.
import { Extension, EXTENSION_POINT } from "https://cdn.jsdelivr.net/npm/cm-chessboard@8.7.8/src/model/Extension.js";

const SVG_NS = "http://www.w3.org/2000/svg";
// Thumbs-up from Material Icons (Apache 2.0), drawn in a 24x24 box
const THUMB_PATH = "M1 21h4V9H1v12zm22-11c0-1.1-.9-2-2-2h-6.31l.95-4.57.03-.32c0-.41-.17-.79-.44-1.06L14.17 1 7.59 7.59C7.22 7.95 7 8.45 7 9v10c0 1.1.9 2 2 2h9c.83 0 1.54-.5 1.84-1.22l3.02-7.05c.09-.23.14-.47.14-.73v-2z";

export const BADGE_STYLE = {
  best: { color: "#81b64c", symbol: "★" },
  excellent: { color: "#96bc4b", symbol: "thumb" },
  good: { color: "#95b776", symbol: "✓" },
  book: { color: "#a88865", symbol: "book" },
  inaccuracy: { color: "#f7c631", symbol: "?!" },
  mistake: { color: "#ffa459", symbol: "?" },
  blunder: { color: "#fa412d", symbol: "??" },
  missed_win: { color: "#ff7769", symbol: "cross" },
};

function svgEl(name, attrs) {
  const node = document.createElementNS(SVG_NS, name);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
  return node;
}

/** Draw a badge centred at (cx, cy) with radius r into an SVG parent. */
export function drawBadge(parent, classification, cx, cy, r) {
  const style = BADGE_STYLE[classification];
  if (!style) return null;
  const g = svgEl("g", { class: "move-badge" });
  // Soft shadow so the badge stands out on any square
  g.append(svgEl("circle", { cx, cy: cy + r * 0.08, r, fill: "rgba(0,0,0,0.25)" }));
  g.append(svgEl("circle", { cx, cy, r, fill: style.color, stroke: "#fff", "stroke-width": r * 0.12 }));

  if (style.symbol === "thumb") {
    const size = r * 1.1;
    g.append(svgEl("path", {
      d: THUMB_PATH,
      fill: "#fff",
      transform: `translate(${cx - size / 2} ${cy - size / 2 - r * 0.03}) scale(${size / 24})`,
    }));
  } else if (style.symbol === "cross") {
    // A plain plus sign rotated 45°: two straight strokes with rounded ends
    const a = r * 0.42;
    const stroke = { stroke: "#fff", "stroke-width": r * 0.3, "stroke-linecap": "round" };
    g.append(svgEl("line", { x1: cx - a, y1: cy - a, x2: cx + a, y2: cy + a, ...stroke }));
    g.append(svgEl("line", { x1: cx - a, y1: cy + a, x2: cx + a, y2: cy - a, ...stroke }));
  } else if (style.symbol === "book") {
    // Simple open book: two white pages
    const s = r * 0.62;
    const page = (dir) => `M${cx},${cy - s * 0.55} L${cx + dir * s},${cy - s * 0.8} L${cx + dir * s},${cy + s * 0.55} L${cx},${cy + s * 0.8} Z`;
    g.append(svgEl("path", { d: page(-1), fill: "#fff" }));
    g.append(svgEl("path", { d: page(1), fill: "#fff", opacity: "0.85" }));
  } else {
    const double = style.symbol.length > 1;
    const gap = double ? r * 0.1 : 0;
    const text = svgEl("text", {
      // letter-spacing also adds space after the last character, so shift right by half of it
      x: cx + gap / 2,
      y: cy + r * 0.04,
      "text-anchor": "middle",
      "dominant-baseline": "central",
      fill: "#fff",
      "font-family": "system-ui, -apple-system, 'Segoe UI', sans-serif",
      "font-weight": "800",
      "font-size": double ? r * 0.95 : r * 1.35,
      "letter-spacing": gap,
    });
    text.textContent = style.symbol;
    g.append(text);
  }
  parent.append(g);
  return g;
}

/** A small standalone badge for HTML (move list). */
export function badgeIcon(classification, size = 16) {
  if (!BADGE_STYLE[classification]) return null;
  const svg = svgEl("svg", { width: size, height: size, viewBox: "0 0 20 20", class: "badge-icon", "aria-hidden": "true" });
  drawBadge(svg, classification, 10, 9.5, 8.5);
  return svg;
}

/** cm-chessboard extension: one badge on the top-right corner of a square. */
export class MoveBadge extends Extension {
  constructor(chessboard) {
    super(chessboard);
    this.badge = null; // {square, classification}
    this.group = svgEl("g", { class: "move-badges" });
    chessboard.view.markersTopLayer.append(this.group); // added after Arrows, so it sits on top
    this.registerExtensionPoint(EXTENSION_POINT.afterRedrawBoard, () => this.draw());
    chessboard.setMoveBadge = (square, classification) => {
      this.badge = square && classification ? { square, classification } : null;
      this.draw();
    };
  }

  draw() {
    this.group.replaceChildren();
    if (!this.badge) return;
    const view = this.chessboard.view;
    const { x, y } = view.squareToPoint(this.badge.square);
    const w = view.squareWidth;
    const r = w * 0.2;
    drawBadge(this.group, this.badge.classification, x + w - r * 0.55, y + r * 0.55, r);
  }
}

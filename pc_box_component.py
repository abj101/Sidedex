"""Interactive Emerald-style PC box, isolated from the page rerun flow."""

import streamlit as st
from sprite_assets import pc_ui_asset


_WALLPAPERS = ("forest", "city", "desert", "savanna", "crag", "volcano", "snow",
               "cave", "beach", "seafloor", "river", "sky", "polkadot", "pokecenter")


_HTML = """<div class="sd-pc-shell">
  <div class="sd-pc-left">
    <div class="sd-pc-details"></div>
  </div>
  <div class="sd-pc-right">
    <div class="sd-pc-title"></div>
    <div class="sd-pc-grid"></div>
  </div>
</div>"""

_JS = """
const lastHovered = new WeakMap();
const titleHandlers = new WeakMap();
const resizeObservers = new WeakMap();
const pendingMoves = new WeakMap();

function addText(parent, tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value ?? "";
  parent.append(node);
  return node;
}

function renderDetail(target, row, compact) {
  target.replaceChildren();
  if (!row) {
    if (compact) {
      const detail = document.createElement("div");
      detail.className = "sd-pc-detail";
      const picture = document.createElement("div");
      picture.className = "sd-pc-picture";
      detail.append(picture);
      addText(detail, "div", "sd-pc-empty", "Your Box is empty.");
      target.append(detail);
    } else {
      addText(target, "div", "sd-pc-empty", "Your Box is empty.");
    }
    return;
  }
  const detail = document.createElement("div");
  detail.className = `sd-pc-detail sd-pc-detail-${row.id} sd-pc-default`;
  detail.style.display = "block";
  const picture = document.createElement("div");
  picture.className = "sd-pc-picture";
  const portrait = document.createElement("img");
  portrait.className = "sd-pc-portrait";
  portrait.src = row.portrait_url || row.mini_url || "";
  portrait.alt = row.name || "Pokémon";
  picture.append(portrait);
  detail.append(picture);

  const info = document.createElement("div");
  info.className = "sd-pc-info";
  addText(info, "strong", "", row.name || "Unknown Pokémon");
  addText(info, "span", "", `Lv ${row.level || "?"}`);
  addText(info, "small", "", `Ability: ${row.ability || "Unknown"}`);
  addText(info, "small", "", `Nature: ${row.nature || "Unknown"}`);
  const item = document.createElement("small");
  item.className = "sd-pc-item";
  item.append(document.createTextNode("Item: "));
  if (row.item_icon_url) {
    const itemIcon = document.createElement("img");
    itemIcon.className = "sd-pc-item-icon";
    itemIcon.src = row.item_icon_url;
    itemIcon.alt = "";
    item.append(itemIcon);
  }
  item.append(document.createTextNode(row.item || "None"));
  info.append(item);
  detail.append(info);
  target.append(detail);
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const shell = parentElement.querySelector(".sd-pc-shell");
  const grid = parentElement.querySelector(".sd-pc-grid");
  const details = parentElement.querySelector(".sd-pc-details");
  if (!shell || !grid || !details) return;

  shell.classList.toggle("sd-pc-compact", Boolean(data.compact));
  const host = parentElement.closest(".st-key-pc-box-card") || parentElement;
  if (resizeObservers.has(parentElement)) resizeObservers.get(parentElement).disconnect();
  shell.style.zoom = "";
  if (!data.compact) {
    const fit = () => { shell.style.zoom = String(Math.min(1, host.clientWidth / 960)); };
    const observer = new ResizeObserver(fit);
    observer.observe(host);
    resizeObservers.set(parentElement, observer);
    fit();
  }

  shell.style.setProperty("--pc-data-panel", `url("${data.data_panel_url}")`);
  shell.style.setProperty("--pc-wallpaper", `url("${data.wallpaper_url}")`);
  const title = parentElement.querySelector(".sd-pc-title");
  title.replaceChildren();
  for (const [label, delta, url] of [["Previous Box", -1, data.arrow_left_url], ["Next Box", 1, data.arrow_right_url]]) {
    if (delta === 1) addText(title, "span", "sd-pc-box-name", `BOX ${data.box_no}`);
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sd-pc-arrow";
    button.dataset.delta = delta;
    button.setAttribute("aria-label", label);
    const icon = document.createElement("img");
    icon.src = url;
    icon.alt = "";
    button.append(icon);
    title.append(button);
  }
  if (titleHandlers.has(title)) title.removeEventListener("click", titleHandlers.get(title));
  const titleHandler = event => {
    const button = event.target.closest(".sd-pc-arrow");
    const x = event.clientX - title.getBoundingClientRect().left;
    const delta = button ? Number(button.dataset.delta) : data.compact ? 0 : x < 64 ? -1 : x > title.clientWidth - 64 ? 1 : 0;
    if (delta) setTriggerValue("box", (data.box_no - 1 + delta + 14) % 14 + 1);
  };
  title.addEventListener("click", titleHandler);
  titleHandlers.set(title, titleHandler);

  const rows = data?.rows ?? [];
  const byId = new Map(rows.map(row => [String(row.id), row]));
  let pendingMove = pendingMoves.get(parentElement);
  if (pendingMove) {
    const movedRow = byId.get(String(pendingMove.id));
    if (data.box_no === pendingMove.box_no && movedRow?.slot === pendingMove.slot) {
      pendingMove.icon.remove();
      pendingMoves.delete(parentElement);
      pendingMove = null;
    }
  }
  let activeId = lastHovered.get(parentElement);
  if (!byId.has(String(activeId))) activeId = data?.hovered_id;
  if (!byId.has(String(activeId))) activeId = data?.selected_id;
  if (!byId.has(String(activeId))) activeId = rows[0]?.id;
  lastHovered.set(parentElement, activeId);

  grid.replaceChildren();
  const bySlot = new Map(rows.map(row => [row.slot, row]));
  for (let slot = 0; slot < 30; slot++) {
    const row = bySlot.get(slot);
    const tile = document.createElement("button");
    tile.type = "button";
    tile.className = `sd-pc-tile${row ? ` sd-pc-tile-${row.id}` : ""}`;
    tile.dataset.slot = slot;
    tile.style.padding = "0";
    tile.setAttribute("aria-label", row ? `Select ${row.name || "Pokémon"}` : `Empty slot ${slot + 1}`);
    if (row) {
      let drag = null;
      let suppressClick = false;
      const icon = document.createElement("img");
      icon.className = "sd-pc-mini";
      icon.src = row.mini_url || "";
      icon.alt = row.name || "Pokémon";
      icon.draggable = false;
      if (String(pendingMove?.id) === String(row.id)) icon.style.visibility = "hidden";
      tile.append(icon);
      const showDetail = () => {
        activeId = row.id;
        lastHovered.set(parentElement, activeId);
        renderDetail(details, row, data.compact);
      };
      tile.addEventListener("pointerenter", showDetail);
      tile.addEventListener("focus", showDetail);
      tile.addEventListener("click", event => {
        if (suppressClick) { event.preventDefault(); return; }
        setTriggerValue("selected", row.id);
      });
      const clearTargets = () => {
        grid.querySelectorAll(".sd-pc-drop-target").forEach(node => node.classList.remove("sd-pc-drop-target"));
      };
      const finishDrag = (destination, movedToTarget) => {
        const current = drag;
        if (!current) return;
        clearTargets();
        if (!current.started) { drag = null; return; }
        const rect = destination.getBoundingClientRect();
        icon.style.transition = "left 140ms cubic-bezier(.2,.8,.2,1), top 140ms cubic-bezier(.2,.8,.2,1)";
        requestAnimationFrame(() => {
          icon.style.left = `${rect.left + (rect.width - current.size) / 2}px`;
          icon.style.top = `${rect.top + (rect.height - current.size) / 2}px`;
        });
        let completed = false;
        const complete = () => {
          if (completed) return;
          completed = true;
          if (movedToTarget) {
            pendingMoves.set(parentElement, {
              id: row.id, slot: Number(destination.dataset.slot), box_no: data.box_no, icon,
            });
            setTriggerValue("move", {id: row.id, slot: Number(destination.dataset.slot), box_no: data.box_no, token: `${Date.now()}-${Math.random()}`});
          } else {
            icon.className = "sd-pc-mini";
            icon.removeAttribute("style");
            current.tile.append(icon);
          }
          drag = null;
        };
        setTimeout(complete, 160);
      };
      tile.addEventListener("pointerdown", event => {
        if (event.button !== 0) return;
        drag = {x: event.clientX, y: event.clientY, started: false, tile};
        tile.setPointerCapture(event.pointerId);
      });
      tile.addEventListener("pointermove", event => {
        if (!drag) return;
        if (!drag.started && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
        if (!drag.started) {
          const rect = icon.getBoundingClientRect();
          drag.started = true;
          drag.size = rect.width;
          icon.className = `sd-pc-dragging${data.compact ? " sd-pc-dragging-compact" : ""}`;
          icon.style.width = `${rect.width}px`;
          icon.style.height = `${rect.height}px`;
          icon.style.left = `${rect.left}px`;
          icon.style.top = `${rect.top}px`;
          document.body.append(icon);
        }
        icon.style.left = `${event.clientX - drag.size / 2}px`;
        icon.style.top = `${event.clientY - drag.size / 2}px`;
        clearTargets();
        document.elementFromPoint(event.clientX, event.clientY)?.closest(".sd-pc-tile")?.classList.add("sd-pc-drop-target");
      });
      tile.addEventListener("pointerup", event => {
        if (!drag) return;
        const moved = drag.started;
        const target = document.elementFromPoint(event.clientX, event.clientY)?.closest(".sd-pc-tile");
        if (moved) {
          suppressClick = true;
          setTimeout(() => { suppressClick = false; }, 0);
          const validTarget = target && Number(target.dataset.slot) !== slot;
          finishDrag(validTarget ? target : tile, Boolean(validTarget));
        } else {
          clearTargets();
          drag = null;
        }
      });
      tile.addEventListener("pointercancel", () => drag && finishDrag(tile, false));
    }
    grid.append(tile);
  }
  renderDetail(details, byId.get(String(activeId)), data.compact);
}
"""

_PC_BOX = st.components.v2.component(
    "sidedex_pc_box",
    html=_HTML,
    js=_JS,
    isolate_styles=False,
)


def _box_changed(key):
    box_no = st.session_state[key].box
    if box_no is not None:
        st.session_state["pc_box_number"] = box_no


def pc_box(rows, selected_id=None, hovered_id=None, *, box_no=1, key="pc-box", compact=False):
    """Render PC rows and return the CCv2 result; click ID is ``result.selected``."""
    payload = []
    for row in rows:
        payload.append({
            "id": row["id"],
            "slot": row["slot"],
            "name": row.get("name") or "Unknown Pokémon",
            "level": row.get("level"),
            "status": row.get("status"),
            "types": list(row.get("types") or []),
            "ability": row.get("ability"),
            "nature": row.get("nature"),
            "item": row.get("item"),
            "item_icon_url": row.get("item_icon_url"),
            "mini_url": row.get("mini_url"),
            "portrait_url": row.get("portrait_url"),
        })
    return _PC_BOX(
        key=key,
        data={"rows": payload, "selected_id": selected_id, "hovered_id": hovered_id,
              "compact": compact,
              "box_no": box_no, "wallpaper_url": pc_ui_asset(f"box_{_WALLPAPERS[box_no - 1]}"),
              "data_panel_url": pc_ui_asset("data_panel"),
              "arrow_left_url": pc_ui_asset("arrow_left"),
              "arrow_right_url": pc_ui_asset("arrow_right")},
        on_selected_change=lambda: None,
        on_box_change=lambda: _box_changed(key),
        on_move_change=lambda: None,
    )

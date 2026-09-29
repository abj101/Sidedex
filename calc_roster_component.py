"""Compact six-slot roster editor for the damage calculator."""

import streamlit as st


_HTML = '<div class="sd-calc-roster" role="list" aria-label="Calculator roster"></div>'

_JS = """
const outsideHandlers = new WeakMap();

function text(parent, tag, className, value) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.textContent = value ?? "";
  parent.append(node);
  return node;
}


function showDeleteMenu(event, onDelete) {
  event.preventDefault();
  event.stopPropagation();
  document.querySelectorAll(".sd-context-menu").forEach(node => node.remove());
  const menu = document.createElement("div");
  menu.className = "sd-context-menu";
  menu.setAttribute("role", "menu");
  const item = document.createElement("button");
  item.type = "button";
  item.className = "sd-picker-option";
  item.setAttribute("role", "menuitem");
  item.textContent = "Delete";
  menu.append(item);
  menu.style.left = `${event.clientX}px`;
  menu.style.top = `${event.clientY}px`;
  document.body.append(menu);
  const close = () => {
    menu.remove();
    document.removeEventListener("pointerdown", onOutside, true);
    document.removeEventListener("keydown", onKey, true);
    window.removeEventListener("scroll", close, true);
  };
  const onOutside = e => { if (!menu.contains(e.target)) close(); };
  const onKey = e => { if (e.key === "Escape") close(); };
  item.onclick = () => { close(); onDelete(); };
  document.addEventListener("pointerdown", onOutside, true);
  document.addEventListener("keydown", onKey, true);
  window.addEventListener("scroll", close, true);
}

function icon(parent, url, alt, className) {
  if (!url) return;
  const node = document.createElement("img");
  node.className = className;
  node.src = url;
  node.alt = alt || "";
  node.loading = "lazy";
  parent.append(node);
  return node;
}

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const root = parentElement.querySelector(".sd-calc-roster");
  if (!root) return;
  root.classList.toggle("sd-calc-roster-readonly", !data.editable_empty);
  outsideHandlers.get(parentElement)?.();
  root.replaceChildren();

  const emit = (name, detail) => setTriggerValue(name, {
    ...detail, token: `${Date.now()}-${Math.random()}`
  });
  const closePickers = () => {
    root.querySelectorAll(".sd-calc-picker").forEach(menu => menu.hidden = true);
    root.querySelectorAll("[aria-haspopup='listbox']").forEach(trigger => trigger.setAttribute("aria-expanded", "false"));
  };

    data.slots.forEach((slotData, index) => {
    const canEdit = Boolean(slotData) || data.editable_empty;
    const card = document.createElement("div");
    card.className = `sd-calc-slot${slotData ? " sd-calc-slot-filled" : " sd-calc-slot-empty"}${!canEdit ? " sd-calc-slot-disabled" : ""}${slotData?.focused ? " sd-calc-slot-focused" : ""}`;
    card.setAttribute("role", "listitem");

    const trigger = document.createElement("button");
    trigger.type = "button";
    trigger.className = "sd-calc-slot-card";
    trigger.setAttribute("aria-label", slotData ? `Focus ${slotData.name || "Pokémon"} in slot ${index + 1}` : `Choose Pokémon for slot ${index + 1}`);
    if (!slotData) {
      trigger.setAttribute("aria-haspopup", "listbox");
      trigger.setAttribute("aria-expanded", "false");
    } else {
      trigger.setAttribute("aria-pressed", String(Boolean(slotData.focused)));
    }
    trigger.dataset.slot = String(index);
    trigger.disabled = !canEdit;

    let suppressClick = false;
    if (slotData && data.editable_empty) {
      trigger.addEventListener("contextmenu", event => showDeleteMenu(event, () => emit("delete", {id: slotData.id})));
      let drag = null;
      const dropAt = (x, y) => {
        const node = document.elementFromPoint(x, y);
        const card = node?.closest(".st-key-calc-own-roster .sd-calc-slot-card");
        if (card) return {el: card, kind: "party"};
        const tile = node?.closest(".sd-pc-tile");
        return tile ? {el: tile, kind: "box"} : null;
      };
      trigger.addEventListener("pointerdown", event => {
        if (event.button !== 0) return;
        drag = {x: event.clientX, y: event.clientY, started: false};
        trigger.setPointerCapture(event.pointerId);
      });
      trigger.addEventListener("pointermove", event => {
        if (!drag) return;
        if (!drag.started && Math.hypot(event.clientX - drag.x, event.clientY - drag.y) < 6) return;
        if (!drag.started) {
          const source = trigger.querySelector(".sd-calc-slot-icon");
          const rect = (source || trigger).getBoundingClientRect();
          drag.size = source ? rect.width : 34;
          drag.ghost = document.createElement("img");
          drag.ghost.className = "sd-calc-dragging";
          drag.ghost.src = slotData.icon_url || "";
          drag.ghost.alt = "";
          drag.ghost.style.width = `${drag.size}px`;
          drag.ghost.style.height = `${drag.size}px`;
          drag.ghost.style.left = `${event.clientX - drag.size / 2}px`;
          drag.ghost.style.top = `${event.clientY - drag.size / 2}px`;
          document.body.append(drag.ghost);
          drag.started = true;
        }
        drag.ghost.style.left = `${event.clientX - drag.size / 2}px`;
        drag.ghost.style.top = `${event.clientY - drag.size / 2}px`;
      });
      const finish = event => {
        if (!drag) return;
        const current = drag;
        drag = null;
        if (!current.started) return;
        suppressClick = true;
        setTimeout(() => { suppressClick = false; }, 0);
        current.ghost.remove();
        const hit = dropAt(event.clientX, event.clientY);
        if (!hit) return;
        if (hit.kind === "party") {
          const target = Number(hit.el.dataset.slot);
          if (target !== index) emit("move", {to: "party", from: index, slot: target});
        } else {
          emit("move", {to: "box", id: slotData.id, box_no: Number(hit.el.dataset.box),
                        slot: Number(hit.el.dataset.slot)});
        }
      };
      trigger.addEventListener("pointerup", finish);
      trigger.addEventListener("pointercancel", () => {
        if (drag?.ghost) drag.ghost.remove();
        drag = null;
      });
    }

    if (slotData) {
      icon(trigger, slotData.icon_url, "", "sd-calc-slot-icon");
      const info = document.createElement("span");
      info.className = "sd-calc-slot-info";
      const title = document.createElement("span");
      title.className = "sd-calc-slot-title";
      text(title, "strong", "sd-calc-slot-name", slotData.name || "Unknown Pokémon");
      const types = document.createElement("span");
      types.className = "sd-calc-slot-types";
      (slotData.types || []).forEach(type => {
        const pill = text(types, "span", "sd-calc-slot-type", type.label || "");
        if (type.color) pill.style.setProperty("--type", type.color);
      });
      title.append(types);
      const detail = document.createElement("span");
      detail.className = "sd-calc-slot-detail";
      text(detail, "span", "sd-calc-slot-level", slotData.level == null ? "Lv ?" : `Lv ${slotData.level}`);
      text(detail, "span", "sd-calc-slot-ability", slotData.ability || "Unknown");
      text(detail, "span", "sd-calc-slot-nature", slotData.nature || "Unknown");
      const heldItem = icon(detail, slotData.item_icon_url, slotData.item_label, "sd-calc-slot-item");
      if (heldItem) heldItem.title = slotData.item_label;
      info.append(title, detail);
      trigger.append(info);
    } else {
      text(trigger, "span", "sd-calc-slot-empty-label", "+");
    }

    const menu = document.createElement("div");
    menu.className = "sd-calc-picker";
    menu.hidden = true;
    menu.setAttribute("role", "dialog");
    menu.setAttribute("aria-label", `Choose Pokémon for slot ${index + 1}`);
    const search = document.createElement("input");
    search.type = "search";
    search.className = "sd-calc-picker-search";
    search.placeholder = "Search Pokémon";
    search.autocomplete = "off";
    search.setAttribute("aria-label", "Search Pokémon");
    const list = document.createElement("div");
    list.className = "sd-calc-picker-options";
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Pokémon choices");
    menu.append(search, list);
    card.append(trigger);
    if (data.editable_empty) card.append(menu);
    let pickerTrigger = trigger;

    const renderOptions = () => {
      const query = search.value.trim().toLowerCase();
      const matches = data.options.filter(option => (option.label || "").toLowerCase().includes(query));
      list.replaceChildren();
      matches.forEach(option => {
        const choice = document.createElement("button");
        choice.type = "button";
        choice.className = "sd-calc-picker-option";
        choice.setAttribute("role", "option");
        choice.dataset.value = String(option.value);
        icon(choice, option.icon_url, "", "sd-calc-picker-icon");
        text(choice, "span", "sd-calc-picker-label", option.label || "Unknown Pokémon");
        choice.onclick = () => {
          closePickers();
          emit("replace", {slot: index, value: option.value});
        };
        list.append(choice);
      });
      if (!matches.length) text(list, "span", "sd-calc-picker-empty", data.options.length ? "No matching Pokémon" : "No Pokémon available");
    };

    const open = () => {
      const opening = menu.hidden;
      closePickers();
      if (!opening) return;
      menu.hidden = false;
      pickerTrigger.setAttribute("aria-expanded", "true");
      search.value = "";
      renderOptions();
      search.focus();
    };
    if (slotData) {
      trigger.onclick = () => { if (!suppressClick) emit("focus", {slot: index}); };
      if (data.editable_empty) {
        const change = document.createElement("button");
        change.type = "button";
        change.className = "sd-calc-slot-change";
        change.setAttribute("aria-label", `Change ${slotData.name || "Pokémon"} in slot ${index + 1}`);
        change.setAttribute("aria-haspopup", "listbox");
        change.setAttribute("aria-expanded", "false");
        change.textContent = "⇄";
        pickerTrigger = change;
        change.onclick = event => { event.stopPropagation(); open(); };
        card.append(change);
      }
    } else {
      trigger.onclick = open;
    }
    search.oninput = renderOptions;
    search.onkeydown = event => {
      if (event.key === "Escape") { closePickers(); trigger.focus(); }
      else if (event.key === "ArrowDown") { event.preventDefault(); list.querySelector(".sd-calc-picker-option")?.focus(); }
    };
    menu.onkeydown = event => {
      if (event.key === "Escape") { closePickers(); trigger.focus(); }
    };
    root.append(card);
  });

  const onOutside = event => { if (!root.contains(event.target)) closePickers(); };
  document.addEventListener("pointerdown", onOutside, true);
  const cleanup = () => document.removeEventListener("pointerdown", onOutside, true);
  outsideHandlers.set(parentElement, cleanup);
  return cleanup;
}
"""

_ROSTER = st.components.v2.component(
    "sidedex_calc_roster",
    html=_HTML,
    js=_JS,
    isolate_styles=False,
)


def calc_roster(*, slots, options, key, editable_empty=True):
    """Render six roster slots; return events as ``result.focus``, ``result.replace`` and ``result.move``."""
    if len(slots) != 6:
        raise ValueError("calc_roster requires exactly six slots")
    return _ROSTER(
        key=key,
        data={
            "slots": slots,
            "options": options,
            "editable_empty": editable_empty,
        },
        on_focus_change=lambda: None,
        on_replace_change=lambda: None,
        on_move_change=lambda: None,
        on_delete_change=lambda: None,
    )

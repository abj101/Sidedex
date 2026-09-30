"""Editable compact status badge for a focused battle Pokémon."""

import streamlit as st


STATUS_COLORS = {
    "none": "#17303a",
    "STATUS1_POISON": "#a647a6",
    "STATUS1_TOXIC_POISON": "#a647a6",
    "STATUS1_BURN": "#e66d39",
    "STATUS1_PARALYSIS": "#b89b22",
    "STATUS1_SLEEP": "#77846a",
    "STATUS1_FREEZE": "#4696d5",
    "STATUS1_FROSTBITE": "#45a5bd",
}

STATUS_TIPS = {
    "STATUS1_POISON": "Loses 1/8 max HP each turn.",
    "STATUS1_TOXIC_POISON": "Loses more HP each turn: 1/16 max HP, then 2/16, 3/16, and so on.",
    "STATUS1_BURN": "Loses 1/16 max HP each turn. Physical damage is halved.",
    "STATUS1_PARALYSIS": "Speed is halved. 25% chance to be unable to move.",
    "STATUS1_SLEEP": "Cannot move for 1-3 turns.",
    "STATUS1_FREEZE": "Cannot move. 20% chance to thaw each turn.",
    "STATUS1_FROSTBITE": "Loses 1/16 max HP each turn. Special damage is halved.",
}

_STATUS_BADGE = st.components.v2.component(
    "sidedex_status_badge",
    html="""<div class="sd-status-control">
      <button class="sd-status-badge" type="button" aria-label="Change status" aria-expanded="false">
        <img class="sd-status-current-icon" alt=""><span class="sd-status-current-label"></span>
      </button>
      <div class="sd-status-menu sd-picker-menu">
        <input type="search" aria-label="Search statuses" placeholder="Search statuses" autocomplete="off">
        <div class="sd-picker-options" role="listbox" aria-label="Statuses"></div>
      </div>
    </div>""",
    js="""
const cleanupByParent = new WeakMap();

export default function ({data, parentElement, setTriggerValue}) {
  cleanupByParent.get(parentElement)?.();
  const root = parentElement.querySelector(".sd-status-control");
  const badge = root.querySelector(".sd-status-badge");
  const icon = root.querySelector(".sd-status-current-icon");
  const label = root.querySelector(".sd-status-current-label");
  const menu = root.querySelector(".sd-status-menu");
  const search = menu.querySelector("input");
  const options = menu.querySelector(".sd-picker-options");
  const choices = data.statuses;
  const current = choices.find(choice => choice.value === data.status) || choices[0];

  const close = () => {
    menu.classList.remove("is-open");
    badge.setAttribute("aria-expanded", "false");
  };
  const render = () => {
    const query = search.value.trim().toLowerCase();
    options.replaceChildren();
    const matches = choices.filter(choice => choice.label.toLowerCase().includes(query));
    for (const choice of matches) {
      const option = document.createElement("button");
      option.type = "button";
      option.className = "sd-picker-option sd-status-option";
      option.setAttribute("role", "option");
      if (choice.tip) option.dataset.tip = choice.tip;
      option.setAttribute("aria-selected", String(choice.value === current.value));
      const text = document.createElement("span");
      text.textContent = choice.label;
      option.append(text);
      option.onclick = () => {
        close();
        if (choice.value !== data.status) {
          setTriggerValue("changed", {value: choice.value, token: `${Date.now()}-${Math.random()}`});
        }
      };
      options.append(option);
    }
    if (!options.childElementCount) {
      const empty = document.createElement("span");
      empty.className = "sd-picker-empty";
      empty.textContent = "No matching statuses";
      options.append(empty);
    }
  };

  badge.style.setProperty("--status-color", current.color);
  if (current.icon) {
    icon.src = current.icon;
    icon.hidden = false;
  } else icon.hidden = true;
  label.textContent = current.icon ? "" : current.label;
  badge.dataset.tip = current.tip || "";
  badge.setAttribute("aria-label", `Status: ${current.label}. Change status`);
  const toggle = () => {
    const opening = badge.getAttribute("aria-expanded") !== "true";
    close();
    if (opening) {
      menu.classList.add("is-open");
      badge.setAttribute("aria-expanded", "true");
      search.value = "";
      render();
      search.focus();
    }
  };
  badge.onpointerdown = event => {
    event.preventDefault();
    event.stopPropagation();
    toggle();
  };
  badge.onclick = event => { if (event.detail === 0) toggle(); };
  search.oninput = render;
  badge.onkeydown = event => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      if (badge.getAttribute("aria-expanded") !== "true") toggle();
      options.querySelector(".sd-status-option")?.focus();
    }
  };
  search.onkeydown = event => {
    if (event.key === "Escape") { close(); badge.focus(); }
    if (event.key === "ArrowDown") { event.preventDefault(); options.querySelector(".sd-status-option")?.focus(); }
  };
  const outside = event => {
    // CCv2 may retarget document-level pointer events to a shadow host.
    const path = event.composedPath?.() || [];
    if (!path.includes(root) && !root.contains(event.target)) close();
  };
  document.addEventListener("pointerdown", outside, true);
  const cleanup = () => document.removeEventListener("pointerdown", outside, true);
  cleanupByParent.set(parentElement, cleanup);
  return cleanup;
}
""",
    isolate_styles=False,
)

_STATUS_OPTIONS = (
    ("none", "None", None),
    ("STATUS1_POISON", "Poison", "poison"),
    ("STATUS1_TOXIC_POISON", "Toxic poison", "poison"),
    ("STATUS1_BURN", "Burn", "burn"),
    ("STATUS1_PARALYSIS", "Paralysis", "paralysis"),
    ("STATUS1_SLEEP", "Sleep", "sleep"),
    ("STATUS1_FREEZE", "Freeze", "freeze"),
    ("STATUS1_FROSTBITE", "Frostbite", "frostbite"),
)


def status_badge(*, status, key):
    """Render the chosen game status and return changes as ``.changed``."""
    from sprite_assets import status_icon

    statuses = [
        {"value": value, "label": label, "icon": status_icon(icon_name),
         "color": STATUS_COLORS[value], "tip": STATUS_TIPS.get(value, "")}
        for value, label, icon_name in _STATUS_OPTIONS
    ]
    return _STATUS_BADGE(
        key=key,
        data={"status": status if status in STATUS_COLORS else "none", "statuses": statuses},
        on_changed_change=lambda: None,
    )

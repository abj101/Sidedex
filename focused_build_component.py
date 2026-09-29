"""Compact editable Level, Nature, and Item chips for the focused Pokémon card."""

import streamlit as st


_HTML = """<div class="sd-build-editor">
  <span class="sd-build-chip sd-build-level">Level <input class="sd-build-level-number" type="number" min="1" max="100" step="1" aria-label="Level"></span>
  <div class="sd-build-combo" data-field="nature">
    <button class="sd-build-chip sd-build-trigger" type="button" aria-label="Change Nature" aria-expanded="false"><span class="sd-build-value"></span></button>
    <input class="sd-build-chip sd-build-edit" type="text" aria-label="Search Nature" autocomplete="off" hidden>
    <div class="sd-build-menu" hidden><div class="sd-build-options" role="listbox" aria-label="Nature"></div></div>
  </div>
  <div class="sd-build-combo" data-field="item">
    <img class="sd-build-item-icon" alt="" hidden>
    <button class="sd-build-chip sd-build-trigger" type="button" aria-label="Change Item" aria-expanded="false"><span class="sd-build-value"></span></button>
    <input class="sd-build-chip sd-build-edit" type="text" aria-label="Search Item" autocomplete="off" hidden>
    <div class="sd-build-menu" hidden><div class="sd-build-options" role="listbox" aria-label="Item"></div></div>
  </div>
</div>"""

_CSS = """
.sd-build-editor { display:flex; align-items:center; gap:6px; min-height:23px; color:#17303a; font:700 11px/1.2 monospace; }
.sd-build-chip { box-sizing:border-box; display:inline-flex; align-items:center; gap:7px; height:23px; padding:3px 5px; color:#17303a; background:#e9eedb; border:1px solid #9bb0a4; border-radius:0; font:inherit; white-space:nowrap; }
.sd-build-level { gap:1ch; }
.sd-build-level:focus-within { outline:1px solid #17303a; outline-offset:1px; }
.sd-build-level-number { box-sizing:content-box; min-width:1ch; height:100%; padding:0; margin:0; color:inherit; background:transparent; border:0; border-radius:0; outline:none; font:inherit; text-align:left; appearance:textfield; -moz-appearance:textfield; }
.sd-build-level-number::-webkit-inner-spin-button,.sd-build-level-number::-webkit-outer-spin-button { -webkit-appearance:none; margin:0; }
.sd-build-trigger { cursor:pointer; max-width:180px; }
.sd-build-trigger[hidden] { display:none; }
.sd-build-trigger:hover,.sd-build-trigger:focus-visible { color:#17303a; background:#e9eedb; border-color:#9bb0a4; outline:1px solid #17303a; outline-offset:1px; }
.sd-build-value { overflow:hidden; text-overflow:ellipsis; }
.sd-build-combo { position:relative; }
.sd-build-combo[data-field="item"] :is(.sd-build-trigger,.sd-build-edit) { padding-left:5px; }
.sd-build-combo[data-field="item"][data-has-icon="true"] :is(.sd-build-trigger,.sd-build-edit) { padding-left:25px; }
.sd-build-edit { width:100%; outline:none; }
.sd-build-edit[hidden] { display:none; }
.sd-build-edit:focus { color:#17303a; background:#e9eedb; border-color:#9bb0a4; outline:none; }
.sd-build-menu { position:absolute; z-index:20; top:calc(100% + 2px); left:0; box-sizing:border-box; width:max-content; min-width:205px; max-width:275px; padding:4px; color:#17303a; background:#e9eedb; border:1px solid #9bb0a4; box-shadow:0 3px 0 #071d24; }
.sd-build-menu[hidden] { display:none; }
.sd-build-options { max-height:184px; overflow-y:auto; }
.sd-build-option { box-sizing:border-box; display:flex; justify-content:space-between; gap:8px; width:100%; min-height:24px; padding:4px 6px; color:#17303a; background:transparent; border:0; border-radius:0; text-align:left; font:700 11px/1.2 monospace; cursor:pointer; }
.sd-build-option:hover,.sd-build-option:focus-visible { color:#17303a; background:#d8e5dc; outline:none; }
.sd-build-effect { color:#49616a; white-space:nowrap; }
.sd-build-item-icon { position:absolute; z-index:1; top:4px; left:5px; width:16px; height:16px; object-fit:contain; image-rendering:pixelated; pointer-events:none; }
"""

_JS = """
const outsideHandlers = new WeakMap();

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const root = parentElement.querySelector(".sd-build-editor");
  if (!root) return;
  outsideHandlers.get(parentElement)?.();

  const levelEdit = root.querySelector(".sd-build-level-number");
  levelEdit.value = data.level ?? "";
  root.querySelector(".sd-build-level").onclick = () => { levelEdit.focus(); levelEdit.select(); };
  const sizeLevel = () => { levelEdit.style.width = `${Math.min(3, Math.max(1, levelEdit.value.length))}ch`; };
  sizeLevel();
  let levelTimer;
  const commitLevel = () => {
    clearTimeout(levelTimer);
    const value = Number(levelEdit.value);
    if (Number.isInteger(value) && value >= 1 && value <= 100 && value !== Number(data.level)) {
      setTriggerValue("changed", {field: "level", value, token: `${Date.now()}-${Math.random()}`});
    } else if (!Number.isInteger(value) || value < 1 || value > 100) {
      levelEdit.value = data.level ?? "";
      sizeLevel();
    }
  };
  levelEdit.oninput = () => { sizeLevel(); clearTimeout(levelTimer); levelTimer = setTimeout(commitLevel, 450); };
  levelEdit.onkeydown = event => {
    if (event.key === "Enter") { event.preventDefault(); commitLevel(); levelEdit.blur(); }
    if (event.key === "Escape") { clearTimeout(levelTimer); levelEdit.value = data.level ?? ""; sizeLevel(); levelEdit.blur(); }
  };
  levelEdit.onblur = commitLevel;
  const icon = root.querySelector(".sd-build-item-icon");
  const itemCombo = root.querySelector('.sd-build-combo[data-field="item"]');
  const previewItem = choice => {
    icon.hidden = !choice?.icon;
    itemCombo.dataset.hasIcon = String(Boolean(choice?.icon));
    if (choice?.icon) icon.src = choice.icon;
    icon.alt = choice?.icon ? `${choice.label} icon` : "";
  };
  previewItem({icon: data.item_icon, label: data.item_label});

  const combos = [...root.querySelectorAll(".sd-build-combo")];
  const closeAll = () => {
    previewItem({icon: data.item_icon, label: data.item_label});
    for (const combo of combos) {
      combo.querySelector(".sd-build-menu").hidden = true;
      const trigger = combo.querySelector(".sd-build-trigger");
      trigger.hidden = false;
      trigger.setAttribute("aria-expanded", "false");
      combo.querySelector(".sd-build-edit").hidden = true;
      combo.style.width = "";
    }
  };

  for (const combo of combos) {
    const field = combo.dataset.field;
    const choices = data[`${field}_options`] || [];
    const current = data[field] || "";
    const trigger = combo.querySelector(".sd-build-trigger");
    const menu = combo.querySelector(".sd-build-menu");
    const search = combo.querySelector(".sd-build-edit");
    const list = combo.querySelector(".sd-build-options");
    trigger.querySelector(".sd-build-value").textContent = field === "nature" ? data.nature_label : data.item_label;

    const commit = value => {
      closeAll();
      if (value !== current) {
        setTriggerValue("changed", {field, value, token: `${Date.now()}-${Math.random()}`});
      }
    };
    const renderChoices = (queryText = search.value) => {
      const query = queryText.trim().toLowerCase();
      const matches = choices.filter(choice => choice.label.toLowerCase().includes(query));
      list.replaceChildren();
      for (const choice of matches) {
        const option = document.createElement("button");
        option.type = "button";
        option.className = "sd-build-option";
        option.setAttribute("role", "option");
        option.setAttribute("aria-selected", String(choice.value === current));
        const label = document.createElement("span");
        label.textContent = choice.label;
        option.append(label);
        if (choice.effect) {
          const effect = document.createElement("span");
          effect.className = "sd-build-effect";
          effect.textContent = choice.effect;
          option.append(effect);
        }
        if (field === "item") {
          option.onpointerenter = () => previewItem(choice);
          option.onfocus = () => previewItem(choice);
        }
        option.onclick = () => commit(choice.value);
        list.append(option);
      }
      if (!matches.length && query) {
        const custom = document.createElement("button");
        custom.type = "button";
        custom.className = "sd-build-option";
        custom.textContent = `Use “${search.value.trim()}”`;
        custom.onclick = () => commit(search.value.trim());
        list.append(custom);
      }
    };
    trigger.onclick = () => {
      const opening = menu.hidden;
      closeAll();
      if (opening) {
        combo.style.width = `${trigger.getBoundingClientRect().width}px`;
        trigger.hidden = true;
        search.hidden = false;
        menu.hidden = false;
        trigger.setAttribute("aria-expanded", "true");
        search.value = field === "nature" ? data.nature_label : data.item_label;
        renderChoices("");
        search.focus();
        search.select();
      }
    };
    search.oninput = () => renderChoices();
    search.onkeydown = event => {
      if (event.key === "Escape") { closeAll(); trigger.focus(); }
      if (event.key === "Enter") {
        event.preventDefault();
        const query = search.value.trim().toLowerCase();
        const match = choices.find(choice => choice.label.toLowerCase() === query)
          || choices.find(choice => choice.label.toLowerCase().includes(query));
        if (match) commit(match.value);
        else if (query) commit(search.value.trim());
      }
    };
    trigger.onkeydown = event => {
      if (event.key === "ArrowDown") { event.preventDefault(); trigger.click(); }
      if (event.key === "Escape") closeAll();
    };
  }

  const onOutside = event => { if (!root.contains(event.target)) closeAll(); };
  document.addEventListener("pointerdown", onOutside, true);
  const cleanup = () => { clearTimeout(levelTimer); document.removeEventListener("pointerdown", onOutside, true); };
  outsideHandlers.set(parentElement, cleanup);
  return cleanup;
}
"""

_FOCUSED_BUILD = st.components.v2.component(
    "sidedex_focused_build",
    html=_HTML,
    css=_CSS,
    js=_JS,
    isolate_styles=False,
)


def focused_build(*, level, nature, nature_label, nature_options, item, item_label, item_options, item_icon_url, key):
    return _FOCUSED_BUILD(
        key=key,
        data={"level": level, "nature": nature, "nature_label": nature_label,
              "nature_options": nature_options, "item": item, "item_label": item_label,
              "item_options": item_options, "item_icon": item_icon_url},
        on_changed_change=lambda: None,
    )

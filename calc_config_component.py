"""Inline save and load picker for Calc team configurations."""

import streamlit as st


_HTML = '<div class="sd-config-picker"></div>'

_JS = """
const outsideHandlers = new WeakMap();

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const root = parentElement.querySelector(".sd-config-picker");
  if (!root) return;
  outsideHandlers.get(parentElement)?.();
  const state = {open: false};
  root.replaceChildren();

  const emit = (name, detail) => setTriggerValue(name, {
    ...detail, token: `${Date.now()}-${Math.random()}`
  });
  const trigger = document.createElement("button");
  trigger.type = "button";
  trigger.className = "sd-config-trigger";
  trigger.setAttribute("aria-label", "Save or load team configs");
  trigger.setAttribute("aria-haspopup", "dialog");
  trigger.setAttribute("aria-expanded", String(state.open));
  trigger.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 3h12l4 4v14H3V3h2zm0 2v14h14V8.2L16.2 5H16v6H7V5H5zm4 0v4h5V5H9zm-2 9h10v5H7v-5z"/></svg>';

  const menu = document.createElement("div");
  menu.className = "sd-calc-picker sd-config-menu";
  menu.hidden = !state.open;
  menu.setAttribute("role", "dialog");
  menu.setAttribute("aria-label", "Saved team configs");

  const form = document.createElement("form");
  form.className = "sd-config-form";
  const input = document.createElement("input");
  input.className = "sd-calc-picker-search";
  input.type = "text";
  input.maxLength = 60;
  input.placeholder = "Name this config";
  input.setAttribute("aria-label", "Config name");
  const usedNames = new Set((data.configs || []).map(config => config.name.toLowerCase()));
  let nextNumber = 1;
  while (usedNames.has(`config${nextNumber}`)) nextNumber++;
  input.value = `Config${nextNumber}`;
  const confirm = document.createElement("button");
  confirm.type = "submit";
  confirm.className = "sd-config-confirm";
  confirm.setAttribute("aria-label", "Save config");
  confirm.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M3 9h5v5H3zm4 4h5v5H7zm4-4h5v5h-5zm4-4h5v5h-5zm4-4h5v5h-5z"/></svg>';
  form.append(input, confirm);
  form.onsubmit = event => {
    event.preventDefault();
    const name = input.value.trim();
    if (name) emit("save", {name});
    else input.focus();
  };
  menu.append(form);

  const options = document.createElement("div");
  options.className = "sd-calc-picker-options";
  options.setAttribute("role", "list");
  for (const config of data.configs || []) {
    const row = document.createElement("div");
    row.className = "sd-config-row";
    row.setAttribute("role", "listitem");
    const load = document.createElement("button");
    load.type = "button";
    load.className = "sd-calc-picker-option";
    load.textContent = config.name;
    load.setAttribute("aria-label", `Load ${config.name}`);
    load.onclick = () => emit("load", {id: config.id});
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "sd-config-delete";
    remove.setAttribute("aria-label", `Delete ${config.name}`);
    remove.textContent = "×";
    remove.onclick = () => emit("delete", {id: config.id});
    row.append(load, remove);
    options.append(row);
  }
  if (!options.children.length) {
    const empty = document.createElement("span");
    empty.className = "sd-calc-picker-empty";
    empty.textContent = "No saved configs yet";
    options.append(empty);
  }
  menu.append(options);
  root.append(trigger, menu);

  const close = () => { state.open = false; menu.hidden = true; trigger.setAttribute("aria-expanded", "false"); };
  trigger.onclick = () => {
    state.open = !state.open;
    menu.hidden = !state.open;
    trigger.setAttribute("aria-expanded", String(state.open));
    if (state.open) input.focus();
  };
  menu.onkeydown = event => { if (event.key === "Escape") { close(); trigger.focus(); } };
  const onOutside = event => { if (!root.contains(event.target)) close(); };
  document.addEventListener("pointerdown", onOutside, true);
  const cleanup = () => document.removeEventListener("pointerdown", onOutside, true);
  outsideHandlers.set(parentElement, cleanup);
  return cleanup;
}
"""

_PICKER = st.components.v2.component(
    "sidedex_calc_configs", html=_HTML, js=_JS, isolate_styles=False,
)


def calc_config_picker(*, configs, key):
    return _PICKER(
        key=key,
        data={"configs": configs},
        on_save_change=lambda: None,
        on_load_change=lambda: None,
        on_delete_change=lambda: None,
    )

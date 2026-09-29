"""Interactive ability and move controls in the focused Pokémon profile."""

import streamlit as st


_PROFILE = st.components.v2.component(
    "sidedex_focused_profile",
    html="""<div class="sd-profile-root"></div>""",
    js="""
const outsideHandlers = new WeakMap();

export default function (component) {
  const { data, parentElement, setTriggerValue } = component;
  const root = parentElement.querySelector(".sd-profile-root");
  if (!root) return;
  outsideHandlers.get(parentElement)?.();
  root.innerHTML = data.markup;

  const emit = (field, value, slot) => setTriggerValue("changed", {
    field, value, slot, token: `${Date.now()}-${Math.random()}`
  });
  let resetAbilityDescription = () => {};
  let shouldCloseAbilityMenu = () => false;
  const closeMenus = () => {
    root.querySelectorAll(".sd-picker-menu").forEach(menu => menu.hidden = true);
    root.querySelectorAll("[aria-expanded]").forEach(button => button.setAttribute("aria-expanded", "false"));
    resetAbilityDescription();
  };
  const ability = root.querySelector(".sd-ability-editor");
  if (ability) {
    const trigger = ability.querySelector(".sd-ability-trigger");
    const menu = ability.querySelector(".sd-picker-menu");
    const description = ability.querySelector(".sd-ability-description");
    const selectedChoice = data.abilities.find(choice => choice.value === data.ability);
    let selectedLabel = selectedChoice?.label || trigger.textContent;
    let selectedDescription = data.abilities.find(choice => choice.value === data.ability)?.description
      || (data.ability ? "Description unavailable." : "Choose an ability to see its effect.");
    const showDescription = value => { description.textContent = value || "Description unavailable."; };
    const showChoice = choice => {
      trigger.textContent = choice.label;
      showDescription(choice.description);
    };
    resetAbilityDescription = () => {
      trigger.textContent = selectedLabel;
      showDescription(selectedDescription);
    };
    shouldCloseAbilityMenu = event => !menu.hidden && !menu.contains(event.target) && event.target !== trigger;
    trigger.onclick = () => {
      const opening = menu.hidden;
      closeMenus();
      menu.hidden = !opening;
      trigger.setAttribute("aria-expanded", String(opening));
    };
    trigger.onmouseenter = resetAbilityDescription;
    menu.onpointerleave = resetAbilityDescription;
    for (const choice of data.abilities) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "sd-picker-option";
      button.setAttribute("role", "option");
      button.textContent = choice.label;
      button.setAttribute("aria-selected", String(choice.value === data.ability));
      button.onpointerenter = button.onfocus = () => showChoice(choice);
      button.onclick = () => {
        selectedLabel = choice.label;
        selectedDescription = choice.description;
        closeMenus();
        if (choice.value !== data.ability) emit("ability", choice.value);
      };
      menu.append(button);
    }
  }

  for (const card of root.querySelectorAll(".sd-move-card")) {
    const slot = Number(card.dataset.slot);
    const remove = card.querySelector(".sd-move-delete");
    if (remove) remove.onclick = () => emit("move_remove", "", slot);
    const add = card.querySelector(".sd-move-add");
    const menu = card.querySelector(".sd-picker-menu");
    const search = menu.querySelector("input");
    const list = menu.querySelector(".sd-picker-options");
    const renderOptions = () => {
      const query = search.value.trim().toLowerCase();
      list.replaceChildren();
      const currentMove = card.dataset.moveId;
      const available = data.moves.filter(choice => !data.known_moves.includes(choice.value) || choice.value === currentMove);
      for (const choice of available.filter(choice =>
        [choice.label, choice.type, choice.power, choice.accuracy, choice.pp, choice.description]
          .some(value => String(value ?? "").toLowerCase().includes(query)))) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "sd-picker-option sd-move-option";
        button.setAttribute("role", "option");
        const name = document.createElement("strong");
        name.textContent = choice.label;
        const values = document.createElement("small");
        values.textContent = [choice.type, `Power ${choice.power ?? "—"}`,
          `Accuracy ${choice.accuracy ?? "—"}`, `${choice.pp ?? "—"} PP`].join(" · ");
        const description = document.createElement("span");
        description.textContent = choice.description || "Description unavailable.";
        button.append(name, values, description);
        button.onclick = () => { closeMenus(); emit(add ? "move_add" : "move_replace", choice.value, slot); };
        list.append(button);
      }
      if (!list.childElementCount) {
        const empty = document.createElement("span");
        empty.className = "sd-picker-empty";
        empty.textContent = available.length ? "No matching moves" : "No moves available";
        list.append(empty);
      }
    };
    const openPicker = trigger => {
      if (data.locked) return;
      const opening = menu.hidden;
      closeMenus();
      menu.hidden = !opening;
      trigger.setAttribute("aria-expanded", String(opening));
      if (opening) { search.value = ""; renderOptions(); search.focus(); }
    };
    if (add) {
      add.onclick = () => openPicker(add);
    } else {
      card.onclick = event => {
        if (event.target.closest(".sd-move-delete, .sd-picker-menu")) return;
        openPicker(card);
      };
      card.onkeydown = event => {
        if (event.target !== card || !["Enter", " "].includes(event.key)) return;
        event.preventDefault();
        openPicker(card);
      };
    }
    search.oninput = renderOptions;
    search.onkeydown = event => { if (event.key === "Escape") { closeMenus(); (add || card).focus(); } };
  }

  const onOutside = event => {
    if (shouldCloseAbilityMenu(event)) {
      closeMenus();
      return;
    }
    if (!root.contains(event.target)) closeMenus();
  };
  document.addEventListener("pointerdown", onOutside, true);
  const cleanup = () => document.removeEventListener("pointerdown", onOutside, true);
  outsideHandlers.set(parentElement, cleanup);
  return cleanup;
}
""",
    isolate_styles=False,
)


def focused_profile(*, markup, ability, abilities, known_moves, moves, key, locked=False):
    return _PROFILE(
        key=key,
        data={"markup": markup, "ability": ability, "abilities": abilities,
              "known_moves": known_moves, "moves": moves, "locked": locked},
        on_changed_change=lambda: None,
    )

"""Inline current HP editor for the focused battle Pokémon."""

import streamlit as st


_HP_BADGE = st.components.v2.component(
    "sidedex_hp_badge",
    html="""<span class="sd-hp-badge"><input type="number" min="0" step="1" aria-label="Current HP"><span class="sd-hp-maximum"></span></span>""",
    css="""
.sd-hp-badge { display:inline-flex; align-items:center; box-sizing:border-box; min-height:23px; padding:3px 6px; color:#17303a; background:#e9eedb; border:1px solid #9bb0a4; font:700 11px/1.2 monospace; white-space:nowrap; cursor:text; }
.sd-hp-badge { position:relative; left:-2px; vertical-align:middle; justify-content:center; height:23px; }
.sd-hp-badge:focus-within,.sd-hp-badge:hover { outline:1px solid #17303a; outline-offset:1px; }
.sd-hp-badge input { box-sizing:content-box; min-width:1ch; height:1.2em; padding:0; margin:0; color:inherit; background:transparent; border:0; border-radius:0; outline:none; font:inherit; text-align:left; appearance:textfield; -moz-appearance:textfield; }
.sd-hp-badge input::-webkit-inner-spin-button,.sd-hp-badge input::-webkit-outer-spin-button { -webkit-appearance:none; margin:0; }
""",
    js="""
export default function ({data, parentElement, setTriggerValue}) {
  const badge = parentElement.querySelector(".sd-hp-badge");
  const edit = badge.querySelector("input");
  const maximum = Number.isInteger(data.maximum) && data.maximum > 0 ? data.maximum : null;
  edit.value = data.current ?? "";
  edit.disabled = maximum === null;
  if (maximum !== null) edit.max = maximum;
  badge.querySelector(".sd-hp-maximum").textContent = `/${maximum ?? "?"} HP`;
  const size = () => { edit.style.width = `${Math.max(1, edit.value.length)}ch`; };
  const reset = () => { edit.value = data.current ?? ""; size(); };
  size();
  badge.onclick = () => { if (!edit.disabled) { edit.focus(); edit.select(); } };
  let timer;
  const commit = () => {
    clearTimeout(timer);
    const value = Number(edit.value);
    if (edit.value !== "" && Number.isInteger(value) && value >= 0 && value <= maximum) {
      if (value !== data.current) setTriggerValue("changed", {value, token: `${Date.now()}-${Math.random()}`});
    } else reset();
  };
  edit.oninput = () => { size(); clearTimeout(timer); timer = setTimeout(commit, 800); };
  edit.onkeydown = event => {
    if (event.key === "Enter") { event.preventDefault(); commit(); edit.blur(); }
    if (event.key === "Escape") { clearTimeout(timer); reset(); edit.blur(); }
  };
  edit.onblur = commit;
  return () => clearTimeout(timer);
}
""",
    isolate_styles=False,
)


def hp_badge(*, current, maximum, key):
    return _HP_BADGE(key=key, data={"current": current, "maximum": maximum},
                     on_changed_change=lambda: None)

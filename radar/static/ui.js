"use strict";

// Bootstrap's CSS is local; interactive controls use the application's existing JS.
window.RadarUI = {
  enhance(root = document) {
    for (const control of root.querySelectorAll("input:not([type=checkbox]):not([type=radio]), textarea")) control.classList.add("form-control");
    for (const control of root.querySelectorAll("select")) control.classList.add("form-select");
    for (const button of root.querySelectorAll(".button")) button.classList.add("btn");
    if (window.lucide) window.lucide.createIcons({root, attrs: {"stroke-width": 1.7}});
  },
  icon(name) {
    const icon = document.createElement("i");
    icon.dataset.lucide = name;
    icon.setAttribute("aria-hidden", "true");
    return icon;
  },
};
window.RadarUI.enhance();

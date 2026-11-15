// layout.js - Drag-to-resize sidebar & right drawer panels with localStorage persistence

const STORAGE_SIDEBAR = "companion.sidebarWidth";
const STORAGE_DRAWER = "companion.drawerWidth";

export function initLayoutResizers() {
  const shell = document.getElementById("app-shell");
  const leftResizer = document.getElementById("resizer-left");
  const rightResizer = document.getElementById("resizer-right");
  const rootStyle = document.documentElement.style;

  const savedLeft = Number(localStorage.getItem(STORAGE_SIDEBAR));
  if (savedLeft >= 220 && savedLeft <= 520) {
    rootStyle.setProperty("--sidebar-width", `${savedLeft}px`);
  }
  const savedRight = Number(localStorage.getItem(STORAGE_DRAWER));
  if (savedRight >= 250 && savedRight <= 680) {
    rootStyle.setProperty("--drawer-width", `${savedRight}px`);
  }

  attachDrag(leftResizer, shell, (ev) => {
    const clamped = Math.max(220, Math.min(520, ev.clientX));
    rootStyle.setProperty("--sidebar-width", `${clamped}px`);
    localStorage.setItem(STORAGE_SIDEBAR, String(clamped));
  });

  attachDrag(rightResizer, shell, (ev) => {
    const width = window.innerWidth - ev.clientX;
    const clamped = Math.max(250, Math.min(680, width));
    rootStyle.setProperty("--drawer-width", `${clamped}px`);
    localStorage.setItem(STORAGE_DRAWER, String(clamped));
  });
}

function attachDrag(handle, shell, onMove) {
  if (!handle) return;
  handle.addEventListener("mousedown", (startEv) => {
    startEv.preventDefault();
    handle.classList.add("active");
    shell.classList.add("is-resizing");

    const moveHandler = (ev) => onMove(ev);
    const upHandler = () => {
      handle.classList.remove("active");
      shell.classList.remove("is-resizing");
      window.removeEventListener("mousemove", moveHandler);
      window.removeEventListener("mouseup", upHandler);
    };

    window.addEventListener("mousemove", moveHandler);
    window.addEventListener("mouseup", upHandler);
  });
}

// layout.js - Drag-to-resize & collapsible sidebar/right drawer with localStorage persistence

import { on } from "./state.js";

const STORAGE_SIDEBAR = "companion.sidebarWidth";
const STORAGE_DRAWER = "companion.drawerWidth";
const STORAGE_SIDEBAR_COLLAPSED = "companion.sidebarCollapsed.v2";
const STORAGE_DRAWER_COLLAPSED = "companion.drawerCollapsed.v2";

export function initLayoutResizers() {
  const shell = document.getElementById("app-shell");
  const leftResizer = document.getElementById("resizer-left");
  const rightResizer = document.getElementById("resizer-right");
  const collapseSidebarBtn = document.getElementById("btn-collapse-sidebar");
  const expandSidebarBtn = document.getElementById("btn-expand-sidebar");
  const collapseDrawerBtn = document.getElementById("btn-collapse-drawer");
  const expandDrawerBtn = document.getElementById("btn-expand-drawer");
  const rootStyle = document.documentElement.style;

  // Clear legacy collapse keys that could trap the old layout in a broken state
  localStorage.removeItem("companion.sidebarCollapsed");
  localStorage.removeItem("companion.drawerCollapsed");

  const savedLeft = Number(localStorage.getItem(STORAGE_SIDEBAR));
  if (savedLeft >= 200 && savedLeft <= 520) {
    rootStyle.setProperty("--sidebar-width", `${savedLeft}px`);
  }
  const savedRight = Number(localStorage.getItem(STORAGE_DRAWER));
  if (savedRight >= 240 && savedRight <= 680) {
    rootStyle.setProperty("--drawer-width", `${savedRight}px`);
  }

  const setSidebarCollapsed = (collapsed, persist = true) => {
    shell.classList.toggle("sidebar-collapsed", Boolean(collapsed));
    if (persist) {
      localStorage.setItem(STORAGE_SIDEBAR_COLLAPSED, collapsed ? "1" : "0");
    }
  };

  const setDrawerCollapsed = (collapsed, persist = true) => {
    shell.classList.toggle("drawer-collapsed", Boolean(collapsed));
    if (persist) {
      localStorage.setItem(STORAGE_DRAWER_COLLAPSED, collapsed ? "1" : "0");
    }
  };

  // On half-screen windows (e.g. 960px side-by-side), start with Files collapsed cleanly into topbar
  const savedLeftCollapsed = localStorage.getItem(STORAGE_SIDEBAR_COLLAPSED);
  const savedRightCollapsed = localStorage.getItem(STORAGE_DRAWER_COLLAPSED);
  const isHalfScreen = window.innerWidth <= 1120;

  setSidebarCollapsed(savedLeftCollapsed === "1", false);
  setDrawerCollapsed(
    savedRightCollapsed !== null ? savedRightCollapsed === "1" : isHalfScreen,
    false
  );

  if (collapseSidebarBtn) {
    collapseSidebarBtn.addEventListener("click", () => setSidebarCollapsed(true, true));
  }
  if (expandSidebarBtn) {
    expandSidebarBtn.addEventListener("click", () => setSidebarCollapsed(false, true));
  }
  if (collapseDrawerBtn) {
    collapseDrawerBtn.addEventListener("click", () => setDrawerCollapsed(true, true));
  }
  if (expandDrawerBtn) {
    expandDrawerBtn.addEventListener("click", () => setDrawerCollapsed(false, true));
  }

  on("drawer:ensure-open", () => setDrawerCollapsed(false, true));

  if (leftResizer) {
    leftResizer.addEventListener("dblclick", () => setSidebarCollapsed(true, true));
  }
  if (rightResizer) {
    rightResizer.addEventListener("dblclick", () => setDrawerCollapsed(true, true));
  }

  attachDrag(leftResizer, shell, (ev) => {
    const clamped = Math.max(200, Math.min(520, ev.clientX));
    rootStyle.setProperty("--sidebar-width", `${clamped}px`);
    localStorage.setItem(STORAGE_SIDEBAR, String(clamped));
  });

  attachDrag(rightResizer, shell, (ev) => {
    const width = window.innerWidth - ev.clientX;
    const clamped = Math.max(240, Math.min(680, width));
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

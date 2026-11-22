// the desktop side menu folded to a strip of icons, remembered in this browser; unfolded by default.

export const SIDE_MENU_KEY = "kd.nav.collapsed";

function stored(): boolean {
  try {
    return localStorage.getItem(SIDE_MENU_KEY) === "1";
  } catch {
    return false; // storage closed (private mode, policy): the menu stays unfolded
  }
}

class SideMenu {
  collapsed = $state(typeof localStorage === "undefined" ? false : stored());

  toggle() {
    this.collapsed = !this.collapsed;
    try {
      if (this.collapsed) localStorage.setItem(SIDE_MENU_KEY, "1");
      else localStorage.removeItem(SIDE_MENU_KEY);
    } catch {
      // not remembered: it still folds for this page
    }
  }
}

export const sideMenu = new SideMenu();

// "Install the app": Chrome/Edge/Android fire beforeinstallprompt early (kept here for the Setup page);
// iOS has no prompt — Setup shows the Share → Add to Home Screen hint instead.

interface InstallPrompt extends Event {
  prompt(): Promise<void>;
  userChoice: Promise<{ outcome: "accepted" | "dismissed" }>;
}

class InstallState {
  prompt = $state<InstallPrompt | null>(null);
  installed = $state(false);
}

export const install = new InstallState();

export function isStandalone(): boolean {
  return (
    matchMedia?.("(display-mode: standalone)").matches ||
    (navigator as Navigator & { standalone?: boolean }).standalone === true
  );
}

export function isIos(): boolean {
  return /iPhone|iPad|iPod/.test(navigator.userAgent);
}

export async function promptInstall() {
  const prompt = install.prompt;
  if (!prompt) return;
  await prompt.prompt();
  const choice = await prompt.userChoice;
  install.prompt = null;
  install.installed = choice.outcome === "accepted";
}

export function watchInstall() {
  addEventListener("beforeinstallprompt", (event) => {
    event.preventDefault();
    install.prompt = event as InstallPrompt;
  });
  addEventListener("appinstalled", () => {
    install.installed = true;
    install.prompt = null;
  });
}

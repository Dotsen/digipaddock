// A notification's target must survive a suspended page, an uncontrolled tab and a signed-out app.
import { safeAppPath } from "./swroutes";

interface WindowTarget {
  url: string;
  navigate(url:string): Promise<WindowTarget | null>;
  focus(): Promise<unknown>;
}
interface Windows {
  matchAll(options:{type:"window"; includeUncontrolled:true}): Promise<readonly WindowTarget[]>;
  openWindow(url:string): Promise<unknown>;
}

export async function openNotification(windows: Windows, origin: string, raw: unknown): Promise<void> {
  const target = new URL(safeAppPath(raw), origin).href;
  const clients = await windows.matchAll({type:"window", includeUncontrolled:true});
  const ours = clients.filter(client => new URL(client.url).origin === origin);
  ours.sort((a,b) => Number(b.url === target) - Number(a.url === target));
  for (const client of ours) {
    let opened: WindowTarget | null;
    try { opened = await client.navigate(target); }
    catch { continue; }
    if (opened) {
      await opened.focus().catch(() => undefined);
      return;
    }
  }
  await windows.openWindow(target);
}

import { expect } from "@playwright/test";
import { getJson, ownerTest as test } from "./helpers";

test("server problems, object tabs and incident explanations fit narrow and wide screens", async ({ page }) => {
  for (const width of [320, 375, 390, 430, 768, 1280]) {
    await page.setViewportSize({width, height:812});
    await page.goto("/servers/web-1");
    await expect(page.getByRole("heading", {name:"web-1", exact:true})).toBeVisible();
    await expect(page.getByRole("button", {name:/Требуют внимания \(1\)/})).toBeVisible();
    await expect(page.getByText("17 d", {exact:true}).first()).toBeVisible();
    await expect(page.locator(".monitor-rows").getByText("gate:disk")).toHaveCount(0);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `server width ${width}`).toBe(true);
    await page.getByRole("tab", {name:/SSH-ключи/}).click();
    await expect(page.getByRole("tabpanel")).toContainText("SHA256");
    await expect(page.getByRole("tabpanel")).not.toContainText("tcp:");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `key width ${width}`).toBe(true);
  }
  await page.setViewportSize({width:375, height:812});
  await page.goto("/servers/web-1");
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-server-mobile.png", fullPage:true});
  await page.goto("/incidents/P-333");
  await expect(page.getByText(/блокирует запуск службы/)).toBeVisible();
  await expect(page.locator("main")).not.toContainText("undefined");
  await expect(page.locator("main")).not.toContainText("{server}");
  // a masked unit is a difference from the baseline: the page asks whether it is the owner's change
  const ask = page.getByRole("region", {name:"Что нужно от вас"});
  await expect(ask).toContainText("Это ваше изменение?");
  // V1: on a phone the question's buttons are on the first screen, above the tab bar
  const tabbar = await page.locator("nav.tabbar").boundingBox();
  for (const name of ["Да, принять", "Нет, разобраться", "Действия"]) {
    const box = await ask.getByRole("button", {name, exact:true}).boundingBox();
    expect(box!.y + box!.height, name).toBeLessThanOrEqual(tabbar!.y);
  }
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-incident.png", fullPage:true});
});

test("the incident's «Действия» menu: line icons, the rows in view on a narrow phone, Escape closes it", async ({page}) => {
  await page.setViewportSize({width:320, height:740});
  await page.goto("/incidents/P-333");
  const menu = page.getByRole("button", {name:"Действия", exact:true});
  await menu.click();
  const rows = page.getByRole("menu", {name:"Действия"}).getByRole("menuitem");
  await expect(rows).toHaveText(["Не напоминать", "Перепроверить сервер", "Связать существующую карточку"]);
  await expect(rows.first()).toBeFocused();
  await expect(rows.first().locator("svg")).toHaveCount(1);
  for (const row of await rows.all()) {
    const box = (await row.boundingBox())!;
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(320);
  }
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-9189-menu-320.png"});
  await page.keyboard.press("ArrowDown");
  await expect(rows.nth(1)).toBeFocused();
  await page.keyboard.press("Escape");
  await expect(page.getByRole("menu")).toHaveCount(0);
  await expect(menu).toBeFocused();
  await menu.click();
  await page.getByRole("menuitem", {name:"Связать существующую карточку"}).click();
  const sheet = page.getByRole("dialog", {name:"Связать существующую карточку"});
  await expect(sheet.getByRole("textbox")).toBeVisible();
  await page.keyboard.press("Escape");
  await expect(sheet).toHaveCount(0);
});

test("an incident handoff creates one linked read-only investigation and keeps it active", async ({page}) => {
  await page.goto("/incidents/P-330");
  const original = await getJson(page, "/api/incidents/P-330");
  if (!original.cards.length) {
    await page.getByRole("button", {name:"Передать сисадмину", exact:true}).click();
    const sheet = page.getByRole("dialog", {name:"Передать сисадмину"});
    await sheet.getByRole("textbox", {name:"Комментарий к разбору"}).fill("Проверить причину и предложить окно без изменений сервера");
    await sheet.getByRole("button", {name:"Создать карточку и передать", exact:true}).click();
    await expect(sheet).toHaveCount(0);
  }
  await expect(page.getByRole("link", {name:/Открыть работу KD-/})).toBeVisible();
  const found = await getJson(page, "/api/incidents/P-330");
  expect(found.cards).toHaveLength(1);
  expect(found.cards[0].assignee).toBe("@sysadmin");
  expect(found.incident.status).not.toBe("resolved");
  await page.getByRole("button", {name:"Действия", exact:true}).click();
  await page.getByRole("menuitem", {name:"Не напоминать"}).click();
  const quiet = page.getByRole("dialog", {name:"Не напоминать"});
  await expect(quiet).toContainText("Об ухудшении всё равно напомним");
  await quiet.getByRole("button", {name:"до закрытия", exact:true}).click();
  await expect(quiet).toHaveCount(0);
  await expect(page.getByRole("status").filter({hasText:"Напоминания выключены до закрытия"})).toBeVisible();
  await page.getByRole("button", {name:"Действия", exact:true}).click();
  await expect(page.getByRole("menuitem", {name:"Включить напоминания"})).toBeVisible();
  await page.keyboard.press("Escape");
  await page.goto("/incidents?level=&server=web-1");
  await expect(page.getByRole("link", {name:/P-330/})).toBeVisible();
  await expect(page.locator("main")).toContainText("Ознакомлен");
});

test("the platform's alerts stand above the incidents, where their push leads, on a phone too", async ({page}) => {
  for (const width of [375, 1280]) {
    await page.setViewportSize({width, height:812});
    await page.goto("/incidents");
    const block = page.getByRole("region", {name:"Тревоги платформы"});
    await expect(block.locator(".platform-alert")).toContainText("factory-1 · сбой digipaddock-mirror.service: kd-mirror: git fetch: timed out");
    await expect(block.locator(".monitor-badge")).toHaveText("Критично");
    await expect(page.getByRole("link", {name:/P-330/})).toBeVisible(); // the incidents are still below
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `alerts width ${width}`).toBe(true);
    await page.screenshot({path:`../../../tmp/cabinet-dev/platform-alerts-${width}.png`});
  }
});

test("accepting signs with the passkey from the sheet at once, then the page says the monitor applies it", async ({page, owner}) => {
  expect(owner.id).toBeTruthy(); // the owner's passkey is on this page
  await page.goto("/incidents/P-334");
  const ask = page.getByRole("region", {name:"Что нужно от вас"});
  const before = await getJson(page, "/api/incidents/P-334");
  if (!before.trust?.some((d: {state:string}) => d.state === "approved")) { // the other project's run signed it already
    await ask.getByRole("button", {name:"Да, принять", exact:true}).click();
    const sheet = page.getByRole("dialog", {name:"Принять изменение в эталон"});
    await sheet.getByRole("button", {name:"Подписать и принять", exact:true}).click();
    await expect(sheet).toHaveCount(0);
    await expect(page.getByRole("dialog")).toHaveCount(0); // no second «sign the decision» dialog
  }
  await expect(ask).toContainText("Вы приняли изменение в");
  await expect(ask.getByRole("button", {name:"Да, принять", exact:true})).toHaveCount(0);
  await ask.getByRole("button", {name:"Действия", exact:true}).click();
  await expect(page.getByRole("menuitem", {name:"Принять изменение в эталон"})).toHaveCount(0);
  const after = await getJson(page, "/api/incidents/P-334");
  const signed = after.trust.filter((d: {state:string}) => d.state === "approved");
  expect(signed).toHaveLength(1);
  expect(signed[0].apply_state).toBe("pending");
});

test("one tick's incidents are one group: picked whole and acknowledged at once (KD-9196)", async ({page}) => {
  await page.setViewportSize({width:320, height:812});
  await page.goto("/incidents");
  const found = await getJson(page, "/api/incidents/query?q=snapd");
  const id: string = found.items[0].group.id;
  expect(found.items[0].group).toMatchObject({size:13, servers:13, subjects:1});
  const group = page.locator("details.incident-group").filter({hasText:"серверов: 13"});
  await expect(group).toHaveCount(1);
  await expect(page.getByRole("link", {name:/P-341/})).toBeHidden();
  await group.locator("summary").click();
  await expect(group.locator(".incident-card")).toHaveCount(13);
  await group.getByLabel("Выбрать всю группу").check();
  await expect(page.getByRole("button", {name:"Принять в эталон (13)"})).toBeEnabled();
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), "bulk bar at 320").toBe(true);
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-9196-group-320.png"});
  await page.getByRole("button", {name:"Подтвердить (13)"}).click();
  await expect(page.locator(".incident-bulk")).toHaveCount(0);
  await expect.poll(async () =>
    (await getJson(page, `/api/incidents/query?status=acknowledged&group=${encodeURIComponent(id)}`)).total).toBe(13);
  await page.setViewportSize({width:1280, height:900});
  await page.goto(`/incidents?group=${encodeURIComponent(id)}`);
  await expect(page.locator(".monitor-note[role=status]")).toContainText("Инцидентов: 13");
  await page.locator("details.incident-group summary").click();
  await expect(page.locator("details.incident-group .incident-card")).toHaveCount(13);
  await expect(page.getByRole("link", {name:"Все 13"})).toHaveCount(0);
});

test.describe("a bundled accept", () => {
test.use({serviceWorkers:"block"});
test("a bundled accept shows what is signed before any signature (KD-9196 review 3)", async ({page}) => {
  await page.route("**/api/monitor-trust/batch", (route) =>
    route.fulfill({json: {decisions: [{id: "d-a"}, {id: "d-b"}], refused: []}}));
  await page.route(/\/api\/decisions\/d-[ab]$/, (route) => {
    const id = route.request().url().split("/").pop()!;
    const n = id === "d-a" ? 341 : 342;
    return route.fulfill({json: {view: {}, decision: {
      id, type: "monitor_trust", card: "", project: "", state: "open", step_up: true,
      decide_permission: "decide.monitor_trust", created: "2026-11-03T10:00:00Z", expires: "", requested_by: "user:owner",
      decided_by: "", decided_at: "", signature: null, reply: "", question: "",
      payload: {kind: "accept", incident: n, server: `grp-0${n - 339}`, check: "sec:units", subject: "snapd.service",
                value: id === "d-a" ? "enabled -> masked" : "23136c1e77fb5fab546d974c5311d1e61a344aea2c9282c456def9c9c4c4c18f",
                bundle: "b1"},
    }}});
  });
  await page.goto("/incidents?q=snapd");
  const group = page.locator("details.incident-group");
  await group.locator("summary").click();
  await group.getByLabel("Выбрать всю группу").check();
  await page.getByRole("button", {name: "Принять в эталон (13)"}).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog).toContainText("объектов: 2, серверов: 2");
  await expect(dialog).toContainText("grp-02: enabled -> masked");
  // a unit file's hash is one long word: it wraps, the dialog does not scroll sideways (stage acceptance)
  await expect(dialog).toContainText("grp-03: 23136c1e77fb");
  expect(await dialog.evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
  await dialog.getByRole("button", {name: "Отмена"}).click();
  await expect(dialog).toHaveCount(0);
});
});

test("server severity labels fit their badge at phone and desktop widths", async ({page}) => {
  for (const width of [320, 375, 430, 768, 1280]) {
    await page.setViewportSize({width, height:812});
    await page.goto("/servers");
    const badge = page.locator(".level.warn").first();
    await expect(badge).toHaveText("Предупреждение");
    expect(await badge.evaluate(el => el.scrollWidth <= el.clientWidth), `badge at ${width}`).toBe(true);
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `list at ${width}`).toBe(true);
  }
  await page.setViewportSize({width:375, height:812});
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-server-list-375.png"});
});

test.describe("incident card presentation", () => {
test.use({serviceWorkers:"block"});

test("a trusted incident asks whether the change is the owner's and accepts it from the question", async ({page}) => {
  await page.goto("/incidents/P-333");
  const original = await getJson(page, "/api/incidents/P-333");
  await page.route(/\/api\/incidents\/P-333$/, route => route.fulfill({json:{...original, acceptable:true, cards:[],
    incident:{...original.incident, status:"open", check:"config", subject:"/etc/ssh/sshd_config", value:"updated"}}}));
  for (const width of [360,1280]) {
    await page.setViewportSize({width,height:812});
    await page.goto("/incidents/P-333");
    const ask = page.getByRole("region", {name:"Что нужно от вас"});
    await expect(ask).toContainText("Это ваше изменение?");
    const button = ask.getByRole("button", {name:"Да, принять",exact:true});
    await expect(button).toHaveAttribute("aria-haspopup", "dialog");
    await expect(ask.getByRole("button", {name:"Нет, разобраться",exact:true})).toBeVisible();
    // the question asks it — the menu does not repeat «accept»
    await ask.getByRole("button", {name:"Действия",exact:true}).click();
    await expect(page.getByRole("menuitem", {name:"Принять изменение в эталон"})).toHaveCount(0);
    await page.keyboard.press("Escape");
    await button.click();
    const sheet = page.getByRole("dialog", {name:"Принять изменение в эталон"});
    await expect(sheet.getByRole("button", {name:"Подписать и принять",exact:true})).toBeVisible();
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await sheet.getByRole("button", {name:"Отмена",exact:true}).click();
    await expect(sheet).toHaveCount(0);
    await expect(button).toBeFocused();
  }
});
test("a login by a key whose access is gone offers «Доступ снят» beside accepting (KD-9272)", async ({page}) => {
  await page.goto("/incidents/P-333");
  const original = await getJson(page, "/api/incidents/P-333");
  await page.route(/\/api\/incidents\/P-333$/, route => route.fulfill({json:{...original, acceptable:true, revocable:true,
    cards:[], incident:{...original.incident, status:"open", check:"logins", subject:"promo@ED25519 SHA256:k/D9",
      value:"с 203.0.113.10 (49 раз)", accepted:null}}}));
  for (const width of [360, 1280]) {
    await page.setViewportSize({width, height:812});
    await page.goto("/incidents/P-333");
    const ask = page.getByRole("region", {name:"Что нужно от вас"});
    await expect(ask).toContainText("Это ваш вход?");
    await expect(ask).toContainText("ключ не станет доверенным");
    await expect(ask.getByRole("button", {name:"Да, принять", exact:true})).toBeVisible();
    const button = ask.getByRole("button", {name:"Доступ снят", exact:true});
    await expect(button).toHaveAttribute("aria-haspopup", "dialog");
    // the question offers it — the menu does not repeat it
    await ask.getByRole("button", {name:"Действия", exact:true}).click();
    await expect(page.getByRole("menuitem", {name:"Доступ снят"})).toHaveCount(0);
    await page.keyboard.press("Escape");
    await button.click();
    const sheet = page.getByRole("dialog", {name:"Доступ снят"});
    await expect(sheet).toContainText("Ключ не станет доверенным");
    await expect(sheet.getByRole("button", {name:"Подписать: доступ снят", exact:true})).toBeVisible();
    await expect(sheet.getByRole("textbox")).toHaveCount(0); // nothing to label: the key is not trusted
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await sheet.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)));
    await page.screenshot({path:`../../../tmp/cabinet-dev/monitoring-9272-revoke-${width}.png`});
    await sheet.getByRole("button", {name:"Отмена", exact:true}).click();
    await expect(sheet).toHaveCount(0);
  }
});
test("a card leading a trusted incident moves «accept» to the menu, tinted", async ({page}) => {
  await page.goto("/incidents/P-333");
  const original = await getJson(page, "/api/incidents/P-333");
  const card = {id:"KD-7", title:"Разбор", status:"In Progress", project:"admin", assignee:"@sysadmin"};
  await page.route(/\/api\/incidents\/P-333$/, route => route.fulfill({json:{...original, acceptable:true, cards:[card],
    incident:{...original.incident, status:"open"}}}));
  await page.goto("/incidents/P-333");
  const ask = page.getByRole("region", {name:"Что нужно от вас"});
  await expect(ask.getByRole("link", {name:"Открыть работу KD-7"})).toBeVisible();
  await ask.getByRole("button", {name:"Действия",exact:true}).click();
  const accept = page.getByRole("menuitem", {name:"Принять изменение в эталон"});
  await expect(accept).toHaveClass(/good/);
  await expect(page.getByRole("menuitem").first()).toHaveText("Принять изменение в эталон");
});
test("a file's difference reads as words: hashes only in the technical details, no login label", async ({page}) => {
  await page.goto("/incidents/P-333");
  const original = await getJson(page, "/api/incidents/P-333");
  const now = "8118".padEnd(64, "a"), baseline = "95e5".padEnd(64, "b");
  await page.route(/\/api\/incidents\/P-333$/, route => route.fulfill({json:{...original, acceptable:true, cards:[],
    incident:{...original.incident, status:"open", check:"sec:cron", subject:"/etc/cron.d/backup", value:now, text:"crontab changed",
      baseline_value:baseline, opened_at:"2026-10-30T09:00:00Z", changes:{count:2, last_at:"2026-10-31T10:00:00Z"},
      current:{...(original.incident.current ?? {}), value:now, text:"crontab changed", at:"2026-11-01T07:00:00Z"}}}}));
  await page.setViewportSize({width:375,height:812});
  await page.goto("/incidents/P-333");
  const ask = page.getByRole("region", {name:"Что нужно от вас"});
  await ask.getByRole("button", {name:"Да, принять",exact:true}).click();
  const sheet = page.getByRole("dialog", {name:"Принять изменение в эталон"});
  await expect(sheet).toContainText("Изменений после открытия: 2");
  await expect(sheet).toContainText("Примется то, что на сервере сейчас");
  await expect(sheet.getByRole("textbox")).toHaveCount(0);
  await expect(sheet.getByText(now)).toBeHidden();
  await sheet.evaluate(el => Promise.all(el.getAnimations().map(a => a.finished)));
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-9189-accept-375.png"});
  await sheet.getByText("Подробнее", {exact:true}).click();
  await expect(sheet.getByText(now)).toBeVisible();
  await page.keyboard.press("Escape");
  for (const region of await page.locator(".monitor-summary").all()) await expect(region).not.toContainText(now);
  const technical = page.locator("details").filter({hasText:"Технические данные"});
  await technical.getByText("Технические данные", {exact:true}).click();
  await expect(technical).toContainText(`Хеш сейчас: ${now}`);
  await expect(technical).toContainText(`Хеш в эталоне: ${baseline}`);
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-9189-drift-375.png", fullPage:true});
});
test("incident cards show the first occurrence, critical styling and open from every surface", async ({page}) => {
  await page.goto("/incidents");
  const seeded = await getJson(page, "/api/incidents/query?level=warn");
  const incident = {...seeded.items.find((i: {id:string}) => i.id === "P-333"), level:"crit", opened_at:"2026-10-13T07:45:00Z"};
  await page.route(/\/api\/incidents\/query(?:\?|$)/, async route => {
    const closed = new URL(route.request().url()).searchParams.get("status") === "resolved";
    await route.fulfill({json:{...seeded, items:[{...incident, status:closed ? "resolved" : "open"}], total:1, counts:{crit:1, warn:0}, next_cursor:null}});
  });
  for (const width of [320, 375, 768, 1280]) {
    await page.setViewportSize({width, height:812});
    await page.goto("/incidents");
    const card = page.getByRole("link", {name:/P-333/});
    await expect(card).toHaveClass(/critical/);
    await expect(card.locator("time")).toHaveAttribute("datetime", incident.opened_at);
    await expect(card.locator("time")).toContainText("13.10.2026");
    await expect(card.locator(".incident-card-top")).toContainText("Впервые обнаружен");
    await expect(card.locator(".incident-card-open")).toHaveText("Открыть инцидент");
    await expect(card.locator("a, button")).toHaveCount(0);
    const style = await card.evaluate(el => {
      const status = getComputedStyle(el.querySelector(".monitor-badge.active")!);
      const severity = getComputedStyle(el.querySelector(".monitor-badge.crit")!);
      const frame = getComputedStyle(el);
      return {status:status.color, severity:severity.color, border:frame.borderTopColor, width:frame.borderTopWidth};
    });
    expect(style.status).toBe(style.severity);
    expect(style.border).toBe(style.status);
    expect(style.width).toBe("2px");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `incident at ${width}`).toBe(true);
  }
  const card = page.getByRole("link", {name:/P-333/});
  await card.locator("p").click();
  await expect(page).toHaveURL(/\/incidents\/P-333$/);
  await page.goto("/incidents");
  await card.locator(".incident-card-open").click();
  await expect(page).toHaveURL(/\/incidents\/P-333$/);
  await page.goto("/incidents");
  await card.press("Enter");
  await expect(page).toHaveURL(/\/incidents\/P-333$/);
  await page.goto("/incidents?status=resolved");
  await expect(card.locator(".monitor-badge.good")).toHaveText("Закрыт");
  await expect(card.locator(".monitor-badge.active")).toHaveCount(0);
  await page.goto("/incidents");
  await page.setViewportSize({width:375, height:812});
  await expect(card).toBeVisible();
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-9124-critical-375.png", fullPage:true});
});
test("incident details show factual clocks, counts and the existing sysadmin investigation", async ({page}) => {
  await page.goto("/incidents/P-333");
  const original = await getJson(page, "/api/incidents/P-333");
  const sample = {...original, incident:{...original.incident, check:"units", subject:"certbot.service",
    value:"failed", opened_at:"2026-10-24T11:40:00Z", confirmation_count:7,
    confirmation_started_at:"2026-10-24T11:40:00Z", confirmation_at:"2026-10-24T12:10:00Z",
    investigation:{state:"reviewed", detail:"", report:{author:"agent:sysadmin", created:"2026-10-24T11:42:00Z",
      thread:1, seq:1, summary:"demo.example: сертификат действует ещё 29 дней.",
      body:"**demo.example: ошибка автопродления сертификата.**\n\nСертификат действует ещё **29 дней**. Причина пока не установлена: журнал certbot недоступен."}}}};
  await page.route(/\/api\/incidents\/P-333$/, route => route.fulfill({json:sample}));
  for (const width of [320,375,768,1280]) {
    await page.setViewportSize({width,height:812});
    await page.goto("/incidents/P-333");
    await expect(page.locator(".monitor-facts")).toContainText("Начало инцидента");
    await expect(page.locator(".monitor-facts")).toContainText("24.10.2026");
    await expect(page.locator(".monitor-facts")).not.toContainText("Подтверждающих проверок");
    await expect(page.locator("details").filter({hasText:"Технические данные"})).toContainText("Подтверждающих проверок: 7");
    await expect(page.locator(".monitor-facts")).toContainText("Последнее подтверждение монитором");
    await expect(page.getByText("Сервер:",{exact:true})).toBeVisible();
    await expect(page.getByRole("region",{name:"Разбор сисадмина"})).toContainText("29 дней");
    await expect(page.locator("main")).not.toContainText("Не передан на разбор");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), `facts at ${width}`).toBe(true);
  }
  await page.getByRole("button",{name:"Действия",exact:true}).click();
  await expect(page.getByRole("menuitem",{name:"Перепроверить сервер"})).toBeVisible();
  await expect(page.getByRole("menuitem",{name:/push/})).toHaveCount(0);
  await page.keyboard.press("Escape");
  await page.setViewportSize({width:375,height:812});
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-9135-facts-375.png",fullPage:true});
});
});

test("incident filters default to any severity and apply every field without a document navigation", async ({page}) => {
  await page.goto("/incidents");
  const level = page.getByRole("combobox", {name:"Серьёзность", exact:true});
  const server = page.getByRole("combobox", {name:"Сервер", exact:true});
  await expect(level).toHaveValue("");
  await expect(page.getByRole("link", {name:/P-333/})).toBeVisible();
  await expect(page.getByRole("link", {name:/P-330/})).toBeVisible();
  await level.selectOption("crit");
  await expect(page).toHaveURL(/level=crit/);
  await expect(page.getByRole("status").filter({hasText:"Инцидентов:"})).toContainText("Инцидентов: 0");
  await page.reload();
  await expect(level).toHaveValue("crit");
  await expect(server.locator("option")).toContainText(["Все серверы", "db-1", "web-1"]);
  await expect(page.getByRole("button", {name:"Найти", exact:true})).toHaveCount(0);
  let documents = 0;
  page.on("request", request => { if (request.isNavigationRequest() && request.frame() === page.mainFrame()) documents++; });
  await level.selectOption("warn");
  await expect(page.getByRole("link", {name:/P-333/})).toBeVisible();
  await server.selectOption("web-1");
  await expect(page.getByRole("link", {name:/P-330/})).toBeVisible();
  await expect(page.getByRole("link", {name:/P-333/})).toHaveCount(0);
  await level.selectOption("");
  expect(new URL(page.url()).searchParams.has("level")).toBe(false);
  await page.getByRole("searchbox", {name:"ID, сервер, проверка или объект"}).fill("P-333");
  await expect(page.getByRole("status").filter({hasText:"Инцидентов:"})).toContainText("Инцидентов: 0");
  await page.getByRole("searchbox", {name:"ID, сервер, проверка или объект"}).fill("");
  await expect(page.getByRole("link", {name:/P-330/})).toBeVisible();
  await page.getByText("Дополнительные фильтры", {exact:true}).click();
  await page.getByRole("combobox", {name:"Обработка", exact:true}).selectOption("assigned");
  await expect(page).toHaveURL(/processing=assigned/);
  await page.getByRole("textbox", {name:"Исполнитель связанной работы", exact:true}).fill("@sysadmin");
  await expect(page).toHaveURL(/assignee=%40sysadmin/);
  await page.getByLabel("Открыты с").fill("2026-01-01");
  await page.getByLabel("Открыты с").press("Tab");
  await expect(page).toHaveURL(/since=2026-01-01/);
  expect(documents).toBe(0);
  await page.reload();
  await expect(level).toHaveValue("");
  await expect(server).toHaveValue("web-1");
  await page.setViewportSize({width:375, height:812});
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  await page.screenshot({path:"../../../tmp/cabinet-dev/monitoring-filters-375.png"});
});

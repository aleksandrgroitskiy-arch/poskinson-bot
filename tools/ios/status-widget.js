// Виджет «Хост poskinson» для iOS-приложения Scriptable. Плоский скрипт: без функций и без шаблонных строк.
const TOPIC = "TOPIC_HERE";
const STALE_MIN = 15;
const GREEN = new Color("#34c759");
const YELLOW = new Color("#ffcc00");
const RED = new Color("#ff453a");
const GRAY = new Color("#8e8e93");

const rows = [];
let at = Date.now();
try {
  const req = new Request("https://ntfy.sh/" + TOPIC + "-status/json?poll=1&since=latest");
  req.timeoutInterval = 15;
  const text = await req.loadString();
  const lines = text.trim().split("\n");
  const msg = JSON.parse(lines[lines.length - 1]);
  const s = JSON.parse(msg.message);
  at = msg.time * 1000;
  const age = Math.round((Date.now() - at) / 60000);
  const stale = age > STALE_MIN;
  rows.push(["Хост", stale ? "молчит " + age + " мин" : "на связи", stale ? RED : GREEN]);
  rows.push(["Бот", s.bot_online ? "онлайн" + (s.version ? " · " + s.version : "") : "ОФЛАЙН", s.bot_online ? GREEN : RED]);
  if (s.battery != null) {
    let bc = GREEN;
    if (!s.charging) { bc = s.battery <= 20 ? RED : YELLOW; }
    rows.push(["Батарея", s.battery + "% " + (s.charging ? "⚡️" : "🔋 не заряжается"), bc]);
  }
  if (s.ram_avail_mb != null) {
    rows.push(["Память", "свободно " + (s.ram_avail_mb / 1024).toFixed(1) + " из " + (s.ram_total_mb / 1024).toFixed(0) + " ГБ", s.ram_avail_mb < 600 ? RED : GREEN]);
  }
  if (s.limit_pct != null) {
    let lc = GREEN;
    if (s.limit_pct >= 85) { lc = RED; } else if (s.limit_pct >= 60) { lc = YELLOW; }
    rows.push(["Лимит", s.limit_pct + "% · ответов " + (s.replies_today != null ? s.replies_today : "?"), lc]);
  }
  if (s.memory_backup_age_h != null) {
    rows.push(["Копия памяти", s.memory_backup_age_h + " ч назад", s.memory_backup_age_h > 30 ? RED : GREEN]);
  }
} catch (e) {
  rows.push(["Связь", "нет данных", RED]);
}

const widget = new ListWidget();
widget.backgroundColor = new Color("#1c1c1e");
widget.setPadding(12, 14, 12, 14);
widget.refreshAfterDate = new Date(Date.now() + 5 * 60 * 1000);
const title = widget.addText("🤖 poskinson");
title.font = Font.boldSystemFont(14);
title.textColor = Color.white();
widget.addSpacer(6);
for (const r of rows) {
  const line = widget.addStack();
  line.centerAlignContent();
  const dot = line.addText("● ");
  dot.font = Font.systemFont(11);
  dot.textColor = r[2];
  const label = line.addText(r[0] + " ");
  label.font = Font.systemFont(12);
  label.textColor = GRAY;
  const value = line.addText(r[1]);
  value.font = Font.semiboldSystemFont(12);
  value.textColor = Color.white();
  widget.addSpacer(3);
}
const foot = widget.addText("обновлено " + new Date(at).toLocaleTimeString("ru-RU", { hour: "2-digit", minute: "2-digit" }));
foot.font = Font.systemFont(9);
foot.textColor = GRAY;

if (config.runsInWidget) {
  Script.setWidget(widget);
} else {
  await widget.presentMedium();
}
Script.complete();

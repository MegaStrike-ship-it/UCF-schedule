/* UCF Schedule — renders data/schedule.json produced by scripts/sync_canvas.py */

const TZ = "America/New_York";
const DAY_NAMES = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"];

let DATA = null;
let LOG = [];
let tab = "today";
let weekOffset = 0;
let eventFilter = "All";

/* ------------------------------------------------------------ campus clock */
/* Everything is computed in campus time so the page reads the same whether
   you open it in Orlando or anywhere else. */

function campusNow() {
  const parts = Object.fromEntries(
    new Intl.DateTimeFormat("en-CA", {
      timeZone: TZ, year: "numeric", month: "2-digit", day: "2-digit",
      hour: "2-digit", minute: "2-digit", hour12: false,
    }).formatToParts(new Date()).map((p) => [p.type, p.value])
  );
  const hour = parts.hour === "24" ? 0 : Number(parts.hour);
  return {
    date: `${parts.year}-${parts.month}-${parts.day}`,
    minutes: hour * 60 + Number(parts.minute),
  };
}

const toMinutes = (hhmm) => {
  const [h, m] = hhmm.split(":").map(Number);
  return h * 60 + m;
};

/* Days between two YYYY-MM-DD strings, ignoring time zones entirely. */
const dayDiff = (from, to) =>
  Math.round((Date.parse(`${to}T00:00:00Z`) - Date.parse(`${from}T00:00:00Z`)) / 86400000);

const shiftDate = (iso, days) =>
  new Date(Date.parse(`${iso}T00:00:00Z`) + days * 86400000).toISOString().slice(0, 10);

const weekdayOf = (iso) => new Date(`${iso}T12:00:00Z`).getUTCDay();

/* Minutes from now until a given date + time, DST-agnostic. */
const minutesUntil = (now, date, hhmm) =>
  dayDiff(now.date, date) * 1440 + toMinutes(hhmm) - now.minutes;

/* ------------------------------------------------------------- formatting */

function fmtTime(hhmm) {
  let [h, m] = hhmm.split(":").map(Number);
  const suffix = h >= 12 ? "pm" : "am";
  h = h % 12 || 12;
  return m ? `${h}:${String(m).padStart(2, "0")}${suffix}` : `${h}${suffix}`;
}

function fmtDay(iso) {
  const d = new Date(`${iso}T12:00:00Z`);
  return `${DAY_NAMES[d.getUTCDay()]}, ${d.toLocaleDateString("en-US", {
    month: "long", day: "numeric", timeZone: "UTC",
  })}`;
}

function fmtCountdown(mins) {
  if (mins < 0) return "";
  if (mins < 1) return "now";
  if (mins < 60) return `in ${mins} min`;
  if (mins < 1440) {
    const h = Math.floor(mins / 60);
    const m = mins % 60;
    return m ? `in ${h}h ${m}m` : `in ${h}h`;
  }
  const days = Math.round(mins / 1440);
  return days === 1 ? "tomorrow" : `in ${days} days`;
}

function fmtStamp(iso) {
  if (!iso) return "";
  const mins = Math.round((Date.now() - Date.parse(iso)) / 60000);
  if (mins < 2) return "synced just now";
  if (mins < 60) return `synced ${mins} min ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `synced ${hrs}h ago`;
  return `synced ${Math.round(hrs / 24)}d ago`;
}

const named = (o) => o.courseTitle || o.course;

const glyph = (icon, size = 18) =>
  `<svg class="wx" width="${size}" height="${size}" aria-hidden="true"><use href="#i-${icon}"/></svg>`;

/* Rain matters to a student only once it's likely enough to change the walk. */
const WET = 30;

function wxChip(w) {
  if (!w) return "";
  const wet = w.precip >= WET;
  return `<span class="wxchip ${wet ? "wet" : ""}">${glyph(w.icon, 14)} ${w.temp}\u00b0${
    wet ? ` \u00b7 ${w.precip}% ${w.label.toLowerCase()}` : ""
  }</span>`;
}

function renderWeather() {
  const el = document.getElementById("weather");
  const w = DATA.weather;
  if (!w) { el.innerHTML = ""; return; }

  const now = campusNow();
  const today = w.days.find((d) => d.date === now.date) || w.days[0];

  const cols = w.days.map((d) => {
    const isToday = d.date === now.date;
    return `<div class="${isToday ? "today-col" : ""}">
      <div class="dow">${isToday ? "Today" : DAY_NAMES[weekdayOf(d.date)].slice(0, 3)}</div>
      <div class="glyph">${glyph(d.icon, 20)}</div>
      <div class="hi">${d.high}\u00b0</div>
      <div class="lo">${d.low}\u00b0</div>
      <div class="pop ${d.precip >= WET ? "wet" : ""}">${d.precip}%</div>
    </div>`;
  }).join("");

  el.innerHTML = `<div class="wxbar">
    <div class="wxnow">
      <span class="glyph">${glyph(w.current.icon, 32)}</span>
      <span class="temp">${w.current.temp}\u00b0</span>
      <span class="desc">${esc(w.current.label)}
        <small>Feels like ${w.current.feelsLike}\u00b0 \u00b7 ${esc(w.place)}</small></span>
      <span class="facts">
        <span>Rain today<b>${today.precip}%</b></span>
        <span>UV<b>${today.uv}</b></span>
        ${w.air ? `<span class="aqi">Air<b>${w.air.aqi} ${esc(w.air.label)}</b></span>` : ""}
        <span>Sunset<b>${fmtTime(today.sunset)}</b></span>
      </span>
    </div>
    <div class="wxdays">${cols}</div>
  </div>`;
}

const esc = (s) =>
  String(s ?? "").replace(/[&<>"]/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

/* ------------------------------------------------------------- components */

function roomHtml(o) {
  const moved = o.changes && o.changes.location;
  if (!moved) return esc(o.location);
  return `<span class="was">${esc(moved.from)}</span> → <b>${esc(moved.to)}</b>`;
}

function rowHtml(o, now) {
  const startsIn = minutesUntil(now, o.date, o.start);
  const endsIn = minutesUntil(now, o.date, o.end);
  const live = startsIn <= 0 && endsIn > 0 && !o.canceled;
  const past = endsIn <= 0;

  const chips = [];
  if (o.canceled) chips.push('<span class="chip">CANCELED</span>');
  if (o.changes && o.changes.location) chips.push('<span class="chip">ROOM CHANGE</span>');
  if (o.changes && o.changes.time) chips.push('<span class="chip">TIME CHANGE</span>');
  if (o.source === "canvas" && !o.meetingId) chips.push('<span class="chip gray">CANVAS</span>');

  const cls = ["row", live && "is-now", past && "is-past", o.canceled && "is-canceled"]
    .filter(Boolean).join(" ");

  return `<div class="${cls}">
    <div class="time">${fmtTime(o.start)}<br>${fmtTime(o.end)}</div>
    <div class="name">${esc(named(o))}${chips.join("")}
      <small>${esc(o.kind)} · ${esc(o.course)}${o.note ? " · " + esc(o.note) : ""}</small>
    </div>
    <div class="place">${roomHtml(o)}${o.weather ? "<br>" + wxChip(o.weather) : ""}</div>
  </div>`;
}

function renderNow() {
  const el = document.getElementById("now");
  const now = campusNow();
  const upcoming = DATA.occurrences
    .filter((o) => !o.canceled && minutesUntil(now, o.date, o.end) > 0)
    .sort((a, b) => (a.date + a.start).localeCompare(b.date + b.start));

  if (!upcoming.length) {
    el.innerHTML = `<div class="label">All done</div>
      <div class="course">No classes left this term</div>`;
    return;
  }

  const next = upcoming[0];
  const startsIn = minutesUntil(now, next.date, next.start);
  const live = startsIn <= 0;
  const after = upcoming[1];

  el.innerHTML = `
    <div class="label ${live ? "is-live" : ""}">${live ? "In class now" : "Next up"}</div>
    <div class="course">${esc(named(next))}</div>
    <div class="kind">${esc(next.kind)} · ${esc(next.course)}</div>
    <div class="meta">
      <span class="room">${roomHtml(next)}</span>
      <span class="when">${fmtTime(next.start)}–${fmtTime(next.end)}${
        next.date === now.date ? "" : " · " + fmtDay(next.date)
      }</span>
      <span class="count ${live ? "is-live" : ""}">${
        live ? `${minutesUntil(now, next.date, next.end)} min left` : fmtCountdown(startsIn)
      }</span>
      ${wxChip(next.weather)}
    </div>
    ${next.canceled ? "" : after ? `<div class="when" style="margin-top:10px;color:var(--ink-3);font-size:13px">
      Then ${esc(named(after))} ${esc(after.kind)} · ${fmtTime(after.start)} · ${esc(after.location)}
    </div>` : ""}`;
}

function renderSevere() {
  return (DATA.severe || []).map((a) => `<div class="severe">
      <div class="head">${esc(a.event)}${
        a.expires ? `<span>until ${fmtTime(a.expires.slice(11, 16))}</span>` : ""
      }</div>
      <p>${esc(a.headline || "")}</p>
    </div>`).join("");
}

function renderBanners() {
  const now = campusNow();
  const horizon = new Set([now.date, shiftDate(now.date, 1), shiftDate(now.date, 2)]);

  const disrupted = DATA.occurrences.filter(
    (o) => horizon.has(o.date) && (o.canceled || (o.changes && Object.keys(o.changes).length))
  );

  const parts = disrupted.map((o) => {
    const what = o.canceled
      ? "is canceled"
      : o.changes.location
      ? `moved to <b>${esc(o.changes.location.to)}</b>`
      : `moved to ${fmtTime(o.start)}–${fmtTime(o.end)}`;
    const when = o.date === now.date ? "today" : fmtDay(o.date).split(",")[0];
    return `<div class="banner"><b>${esc(named(o))} ${esc(o.kind)}</b> ${when} ${what}.</div>`;
  });

  for (const a of (DATA.alerts || []).slice(0, 3)) {
    parts.push(`<div class="banner">
      <b>${esc(a.course)}</b> announcement — ${esc(a.title)}
      <span class="src">${esc(a.excerpt)}${
        a.url ? ` <a href="${esc(a.url)}" target="_blank" rel="noopener">Open in Canvas →</a>` : ""
      }</span></div>`);
  }

  document.getElementById("banners").innerHTML = renderSevere() + parts.join("");
}

/* ------------------------------------------------------------------ views */

function viewToday() {
  const now = campusNow();
  const out = [];

  const todayDue = (DATA.assignments || []).filter(
    (a) => a.overdue || a.dueAt.slice(0, 10) === now.date
  );
  if (todayDue.length) {
    out.push(`<section class="day">
      <h2>Due ${todayDue.some((a) => a.overdue) ? "now" : "today"}</h2>
      <div class="due">${todayDue
        .map((a) => dueRow({ ...a, date: a.dueAt.slice(0, 10), time: a.dueAt.slice(11, 16) }, now))
        .join("")}</div>
    </section>`);
  }

  for (let i = 0; i < 3; i++) {
    const day = shiftDate(now.date, i);
    const rows = DATA.occurrences.filter((o) => o.date === day);
    if (!rows.length && i > 0) continue;

    out.push(`<section class="day">
      <h2>${fmtDay(day)}${i === 0 ? '<span class="today">TODAY</span>' : ""}</h2>
      ${rows.length ? rows.map((o) => rowHtml(o, now)).join("")
                    : '<div class="empty">Nothing scheduled.</div>'}
    </section>`);
  }
  return out.join("");
}

function viewWeek() {
  const now = campusNow();
  const dow = weekdayOf(now.date);
  const monday = shiftDate(now.date, (dow === 0 ? -6 : 1 - dow) + weekOffset * 7);
  const sunday = shiftDate(monday, 6);

  const days = [];
  for (let i = 0; i < 7; i++) {
    const day = shiftDate(monday, i);
    const rows = DATA.occurrences.filter((o) => o.date === day);
    if (!rows.length) continue;
    days.push(`<section class="day">
      <h2>${fmtDay(day)}${day === now.date ? '<span class="today">TODAY</span>' : ""}</h2>
      ${rows.map((o) => rowHtml(o, now)).join("")}
    </section>`);
  }

  const label = `${fmtDay(monday).split(", ")[1]} – ${fmtDay(sunday).split(", ")[1]}`;
  return `<div class="weeknav">
      <button id="wPrev" aria-label="Previous week">‹</button>
      <button id="wNext" aria-label="Next week">›</button>
      <span class="range">${label}${weekOffset === 0 ? " · this week" : ""}</span>
    </div>
    ${days.length ? days.join("") : '<div class="empty">No classes this week.</div>'}`;
}

function dueSoon(now) {
  return (DATA.assignments || []).map((a) => ({
    ...a,
    date: a.dueAt.slice(0, 10),
    time: a.dueAt.slice(11, 16),
  }));
}

function dueRow(a, now) {
  const mins = minutesUntil(now, a.date, a.time);
  const late = a.overdue || mins < 0;
  // The group header already carries the date, so only say something here when
  // it adds information: that it's late, or that it's close.
  const when = late ? "Overdue" : mins < 1440 ? fmtCountdown(mins) : "";

  return `<div class="duerow ${late ? "late" : ""}">
    <div class="task">${
      a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>`
            : esc(a.title)
    }<small>${esc(a.courseTitle || a.course)}${
      a.points ? ` \u00b7 ${a.points} pts` : ""
    }</small></div>
    <div class="clockcol">${when ? `<b>${esc(when)}</b>` : ""}${fmtTime(a.time)}</div>
  </div>`;
}

function viewWork() {
  const now = campusNow();
  const items = dueSoon(now);

  if (!items.length) {
    return `<div class="panel"><h3>Homework</h3>
      <p class="sub">Assignment due dates come from Canvas.</p>
      <div class="empty">${
        DATA.canvas.connected
          ? "Nothing outstanding. Anything already submitted is hidden."
          : "Connect Canvas to see due dates here \u2014 add a CANVAS_TOKEN secret."
      }</div></div>`;
  }

  const late = items.filter((a) => a.overdue);
  const rest = items.filter((a) => !a.overdue);

  const group = (list) => {
    const byDate = {};
    for (const a of list) (byDate[a.date] ||= []).push(a);
    return Object.entries(byDate).map(([date, rows]) => `<section class="day">
      <h2>${fmtDay(date)}${date === now.date ? '<span class="today">TODAY</span>' : ""}</h2>
      <div class="due">${rows.map((a) => dueRow(a, now)).join("")}</div>
    </section>`).join("");
  };

  return `${late.length ? `<section class="day">
      <h2>Overdue</h2><div class="due">${late.map((a) => dueRow(a, now)).join("")}</div>
    </section>` : ""}${group(rest)}`;
}

function daysAway(now, date) {
  const d = dayDiff(now.date, date);
  return d === 0 ? "Today" : d === 1 ? "Tomorrow" : `${d} days`;
}

function viewCampus() {
  const now = campusNow();

  /* --- semester deadlines --- */
  const key = (DATA.keyDates || []).slice(0, 6).map((k) => {
    const away = dayDiff(now.date, k.date);
    return `<li class="${away <= 14 ? "soon" : ""}">
      <span class="d">${fmtDay(k.date).split(", ")[1]}</span>
      <span class="what">${esc(k.label)}${k.note ? `<small>${esc(k.note)}</small>` : ""}</span>
      <span class="away">${daysAway(now, k.date)}</span>
    </li>`;
  }).join("");

  /* --- football --- */
  const games = (DATA.football || []).filter((g) => g.date >= now.date).slice(0, 6);
  const gameList = games.map((g) => `<div class="game ${g.home ? "home" : ""}">
      <div class="gd">${fmtDay(g.date).split(", ")[1].replace(" ", "<br>")}</div>
      <div class="gt">${g.home ? "vs" : "at"} ${esc(g.opponent || "TBA")}${
        g.home ? '<span class="hb">HOME</span>' : ""
      }<small>${esc(g.venue || "")}</small></div>
      <div class="gk">${g.time ? fmtTime(g.time) : "Time TBD"}</div>
    </div>`).join("");

  /* --- student org events, filterable by theme --- */
  const all = DATA.events || [];
  const themes = ["All", ...Array.from(new Set(all.map((e) => e.theme))).sort()];
  const chips = themes.map((t) => `<button data-theme="${esc(t)}"
      aria-pressed="${t === eventFilter}">${esc(t.replace(/([a-z])([A-Z])/g, "$1 $2"))}</button>`).join("");

  const shown = (eventFilter === "All" ? all : all.filter((e) => e.theme === eventFilter)).slice(0, 40);
  const byDate = {};
  for (const e of shown) (byDate[e.date] ||= []).push(e);

  const evSections = Object.entries(byDate).map(([date, rows]) => `<section class="day">
      <h2>${fmtDay(date)}${date === now.date ? '<span class="today">TODAY</span>' : ""}</h2>
      <div class="evlist">${rows.map((e) => `<div class="ev">
        ${e.image
          ? `<img src="${esc(e.image)}" alt="" loading="lazy">`
          : `<div class="noimg">${esc((e.name || "?").trim()[0] || "?")}</div>`}
        <div class="body">
          <a href="${esc(e.url)}" target="_blank" rel="noopener">${esc(e.name)}</a>
          <span class="meta">${esc(e.org || "")}${e.location ? " \u00b7 " + esc(e.location) : ""}</span>
          ${e.blurb ? `<span class="blurb">${esc(e.blurb)}</span>` : ""}
        </div>
        <div class="when">${fmtTime(e.start)}${
          e.rsvps ? `<span class="rsvp">${e.rsvps} going</span>` : ""
        }</div>
      </div>`).join("")}</div>
    </section>`).join("");

  return `${key ? `<div class="panel"><h3>Semester deadlines</h3>
      <p class="sub">From UCF's academic calendar.</p>
      <ul class="keydates">${key}</ul></div>` : ""}

    ${gameList ? `<div class="panel" style="margin-top:14px"><h3>UCF football</h3>
      <p class="sub">Home games are marked \u2014 those are the ones that take over campus.</p>
      <div class="games">${gameList}</div></div>` : ""}

    <div class="panel" style="margin-top:14px">
      <h3>What's on around campus</h3>
      <p class="sub">${all.length} events from KnightConnect over the next three weeks,
        residence-hall events included.</p>
      <div class="filters">${chips}</div>
      ${evSections || '<div class="empty">Nothing listed under this filter.</div>'}
    </div>`;
}

function viewChanges() {
  const entries = LOG.length ? LOG : DATA.recentChanges || [];
  const icons = { room: "📍", time: "🕐", canceled: "🚫", added: "＋", removed: "－" };

  const log = entries.length
    ? `<ul class="log">${entries.slice(0, 40).map((c) => `<li>
        <span>${icons[c.type] || "•"}</span>
        <span><b>${esc(c.courseTitle || c.course)} ${esc(c.kind)}</b> on ${fmtDay(c.date)}<br>
        <span style="color:var(--ink-2)">${esc(c.detail)}</span></span>
        <span class="when" style="margin-left:auto">${esc((c.at || "").slice(0, 10))}</span>
      </li>`).join("")}</ul>`
    : `<div class="empty">No changes recorded yet. Room and time changes will appear here
       as soon as the sync picks them up.</div>`;

  const alerts = (DATA.alerts || []).length
    ? `<div class="panel" style="margin-top:14px">
        <h3>Canvas announcements worth reading</h3>
        <p class="sub">Posts mentioning a cancellation, room change, or reschedule.</p>
        <ul class="log">${DATA.alerts.map((a) => `<li><span>📣</span>
          <span><b>${esc(a.course)}</b> — ${esc(a.title)}<br>
          <span style="color:var(--ink-2)">${esc(a.excerpt)}</span>
          ${a.url ? `<br><a href="${esc(a.url)}" target="_blank" rel="noopener">Open in Canvas →</a>` : ""}</span>
        </li>`).join("")}</ul></div>`
    : "";

  return `<div class="panel"><h3>Change log</h3>
    <p class="sub">Every room, time, and cancellation change the sync has detected.</p>
    ${log}</div>${alerts}`;
}

function viewAbout() {
  const courses = DATA.courses.map((c) =>
    c.url
      ? `<a href="${esc(c.url)}" target="_blank" rel="noopener">${
           c.score != null ? `<span class="score">${c.score}%${c.grade ? " " + esc(c.grade) : ""}</span>` : ""
         }${esc(c.title || c.code)}
         <small>${esc(c.code)} · open in Canvas</small></a>`
      : `<span>${esc(c.title || c.code)}<small>${esc(c.code)} · not linked to Canvas</small></span>`
  ).join("");

  const upcoming = (DATA.assignments || []).length
    ? `<div class="panel" style="margin-top:14px"><h3>Upcoming from Canvas</h3>
       <ul class="log">${DATA.assignments.map((a) => `<li><span>📝</span>
         <span><b>${esc(a.course)}</b> — ${
           a.url ? `<a href="${esc(a.url)}" target="_blank" rel="noopener">${esc(a.title)}</a>` : esc(a.title)
         }</span>
         <span class="when" style="margin-left:auto">${fmtDay(a.dueAt.slice(0, 10)).split(", ")[1]}</span>
       </li>`).join("")}</ul></div>`
    : "";

  return `<div class="panel"><h3>Courses</h3>
      <p class="sub">${DATA.canvas.coursesMatched} of ${DATA.courses.length} matched to a Canvas course.</p>
      <div class="courses">${courses}</div>
    </div>${upcoming}`;
}

/* ----------------------------------------------------------------- wiring */

function render() {
  if (!DATA) return;

  document.getElementById("term").textContent = DATA.term.name;
  document.getElementById("stamp").textContent = fmtStamp(DATA.generatedAt);

  renderNow();
  renderBanners();
  renderWeather();

  const views = { today: viewToday, week: viewWeek, work: viewWork,
                  campus: viewCampus, changes: viewChanges, about: viewAbout };
  document.getElementById("view").innerHTML = views[tab]();

  const work = document.getElementById("workCount");
  const workN = (DATA.assignments || []).length;
  work.hidden = !workN;
  work.textContent = workN > 99 ? "99+" : workN;

  const badge = document.getElementById("changeCount");
  const count = (LOG.length ? LOG : DATA.recentChanges || []).length;
  badge.hidden = !count;
  badge.textContent = count > 99 ? "99+" : count;

  const status = document.getElementById("canvasStatus");
  status.classList.toggle("off", !DATA.canvas.connected);
  status.textContent = DATA.canvas.connected
    ? `Canvas connected — ${DATA.canvas.coursesMatched} course${
        DATA.canvas.coursesMatched === 1 ? "" : "s"
      } syncing from ${DATA.canvas.baseUrl.replace(/^https?:\/\//, "")}`
    : "Canvas not connected — showing the baseline schedule only. Add a CANVAS_TOKEN secret to enable syncing.";

  if (tab === "week") {
    document.getElementById("wPrev").onclick = () => { weekOffset--; render(); };
    document.getElementById("wNext").onclick = () => { weekOffset++; render(); };
  }
  if (tab === "campus") {
    document.querySelectorAll(".filters button").forEach((b) => {
      b.onclick = () => { eventFilter = b.dataset.theme; render(); };
    });
  }
}

document.querySelectorAll("nav.tabs button").forEach((btn) => {
  btn.onclick = () => {
    tab = btn.dataset.tab;
    if (tab === "week") weekOffset = 0;
    document.querySelectorAll("nav.tabs button").forEach((b) =>
      b.setAttribute("aria-selected", String(b === btn)));
    render();
  };
});

async function load() {
  const bust = `?t=${Math.floor(Date.now() / 60000)}`;
  DATA = await (await fetch(`data/schedule.json${bust}`)).json();
  try {
    LOG = await (await fetch(`data/changes.json${bust}`)).json();
  } catch { LOG = []; }

  document.getElementById("icsUrl").textContent =
    new URL("schedule.ics", location.href).href;
  render();
}

load().catch((err) => {
  document.getElementById("now").innerHTML =
    `<div class="label">Error</div><div class="course">Could not load schedule</div>
     <div class="when" style="margin-top:8px">${esc(err.message)}</div>`;
});

setInterval(render, 30000);                 // keep the countdown honest
setInterval(() => load().catch(() => {}), 900000);  // re-check for a new sync
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) load().catch(() => {});
});

/**
 * Нагрузочная проверка перед релизом — docs/07-TESTING-SECURITY.md §3.1.
 *
 * Профиль (§3.1): пик 50 входящих вебхуков/мин (спайк ×5 = 250/мин) +
 * 30 операторов онлайн на WebSocket + фоновое чтение списков и лент +
 * ответы клиентам (уходят через ARQ в Авито/fake-avito).
 *
 * Полный прогон (30 минут):
 *
 *   BASE_URL=https://<хост> ACCOUNT_ID=<uuid> HOOK_SECRET=<секрет> \
 *   AVITO_USER_ID=111222333 SKEW_MS=0 \
 *   OP_USERS='manager@x:pass:send,head@x:pass:read,observer@x:pass:read' \
 *   k6 run tests/load/release.js
 *
 *   RUN=smoke              — четверть нагрузки, 2 минуты (прогон «ничего не развалилось»)
 *   SCENARIOS=operators    — только операторы (вебхуки вбрасываются с сервера, inject.py)
 *   SCENARIOS=incoming     — только вебхуки
 *   INJECT_MODE=fake       — вброс через /_control/incoming мока (нужен FAKE_AVITO_URL)
 *
 * Отступления от листинга §3.1 — все четыре найдены на прогоне и осознанны:
 *
 * 1. `MessageOut` (app/services/conversations.py:79) НЕ содержит `meta`, а
 *    листинг читает `ev.data.message.meta.sent_at`. Метку времени несёт текст
 *    сообщения: `<MARK>-<источник>|<epoch_ms>|<id>`. Побочная польза — та же
 *    метка служит признаком тестовых данных при уборке.
 * 2. HTTP-запросы оператора вынесены из обработчиков сокета в отдельные
 *    сценарии. В листинге `sock.setInterval` дёргает `http.get` внутри цикла
 *    событий k6/ws — измеренная длительность такого запроса включает ожидание
 *    следующего события сокета (на прогоне p95 подскакивал до 19 с при
 *    upstream_response_time 0.07 с в логах nginx). Суммарная нагрузка та же:
 *    30 операторов × (4 списка + 2 ленты)/мин + 30 отправок/мин.
 * 3. Учётки: сидовых op01..op30 в проде нет, 30 VU делят реальные учётки.
 *    Логин ступенчатый — nginx режет /api/v1/auth/login до 10 r/m на IP
 *    (burst=10 без nodelay: одиннадцатый логин в минуту ждёт до 30 с).
 *    Право `messages:send` есть у manager/admin; head и observer — только чтение
 *    (app/core/rbac.py), поэтому отправляют лишь VU с ролью manager.
 * 4. Ответы уходят только в диалоги с меткой MARK: боевые диалоги не трогаем.
 */

import http from "k6/http";
import ws from "k6/ws";
import { check, sleep } from "k6";
import { Counter, Rate, Trend } from "k6/metrics";

// ----------------------------------------------------------------- конфиг ---

const BASE = __ENV.BASE_URL || "https://188-225-34-82.sslip.io";
const WS_BASE = BASE.replace(/^http/, "ws");
const ACCOUNT_ID = __ENV.ACCOUNT_ID || "";
const HOOK_SECRET = __ENV.HOOK_SECRET || "";
const AVITO_USER_ID = Number(__ENV.AVITO_USER_ID || 111222333);

const RUN = __ENV.RUN || "full"; // full | smoke
const SCENARIOS = __ENV.SCENARIOS || "all"; // all | operators | incoming
const INJECT_MODE = __ENV.INJECT_MODE || "hook"; // hook | fake
const FAKE_AVITO_URL = __ENV.FAKE_AVITO_URL || "";

const OPERATORS = Number(__ENV.OPERATORS || 30);
const PEAK_PER_MIN = Number(__ENV.PEAK_PER_MIN || 50);
const SPIKE_PER_MIN = Number(__ENV.SPIKE_PER_MIN || 250);
const CHAT_POOL = Number(__ENV.CHAT_POOL || 50);
const STAGGER_SECONDS = Number(__ENV.STAGGER_SECONDS || 7);
// Часы сервера минус часы машины с k6, мс (для меток серверного инъектора).
const SKEW_MS = Number(__ENV.SKEW_MS || 0);
const MARK = __ENV.MARK || "LOADTEST";

const SMOKE = RUN === "smoke";
const TOTAL_MINUTES = SMOKE ? 2 : 30;
// Сессия сокета: полное окно минус ступенька входа последнего VU.
const SESSION_MS = TOTAL_MINUTES * 60000 - (OPERATORS - 1) * STAGGER_SECONDS * 1000 - 10000;

// email:password:send|read, через запятую
const OP_USERS = (__ENV.OP_USERS || "")
  .split(",")
  .map((s) => s.trim())
  .filter(Boolean)
  .map((s) => {
    const [email, password, rights] = s.split(":");
    return { email, password, canSend: (rights || "read") === "send" };
  });

// -------------------------------------------------------------- метрики -----

const e2eDelivery = new Trend("e2e_delivery_ms", true); // вебхук (k6) -> кадр WS
const e2eServerInjected = new Trend("e2e_delivery_srv_ms", true); // вброс с сервера -> кадр WS
const wsMessages = new Counter("ws_messages_received");
// имя не должно совпадать со встроенными метриками k6/ws (ws_sessions и др.)
const wsSessions = new Counter("op_ws_sessions");
const wsHandshakeFail = new Counter("ws_handshake_failures");
const wsUnexpectedClose = new Counter("ws_unexpected_close");
const hookOk = new Rate("hook_accepted");
const opSent = new Counter("operator_messages_sent");
const opSendFail = new Counter("operator_messages_failed");
const loginFail = new Counter("login_failures");
const loginTime = new Trend("login_ms", true);
const relogins = new Counter("relogins");

// -------------------------------------------------------------- сценарии ----

const incomingStages = SMOKE
  ? [
      { target: Math.round(PEAK_PER_MIN / 4), duration: "30s" },
      { target: Math.round(PEAK_PER_MIN / 4), duration: "90s" },
    ]
  : [
      { target: PEAK_PER_MIN, duration: "5m" }, // разгон
      { target: PEAK_PER_MIN, duration: "20m" }, // плато
      { target: SPIKE_PER_MIN, duration: "2m" }, // спайк ×5
      { target: PEAK_PER_MIN, duration: "3m" }, // спад: очередь обязана разобраться
    ];

// 30 операторов × 4 списка/мин + 2 ленты/мин = 180 чтений/мин; отправок 30/мин.
const READS_PER_MIN = Number(__ENV.READS_PER_MIN || (SMOKE ? 45 : 180));
const SENDS_PER_MIN = Number(__ENV.SENDS_PER_MIN || (SMOKE ? 8 : 30));
const DURATION = `${TOTAL_MINUTES}m`;

const scenarioDefs = {
  incoming: {
    executor: "ramping-arrival-rate",
    startRate: SMOKE ? Math.round(PEAK_PER_MIN / 4) : 5,
    timeUnit: "1m",
    preAllocatedVUs: 4,
    maxVUs: 15,
    stages: incomingStages,
    exec: "incomingWebhook",
    tags: { scenario: "incoming" },
  },
  ws_operators: {
    executor: "constant-vus",
    vus: OPERATORS,
    duration: DURATION,
    exec: "wsOperator",
    tags: { scenario: "ws" },
  },
  operator_reads: {
    executor: "constant-arrival-rate",
    rate: READS_PER_MIN,
    timeUnit: "1m",
    duration: DURATION,
    preAllocatedVUs: 8,
    maxVUs: 20,
    exec: "operatorRead",
    tags: { scenario: "operators" },
  },
  operator_sends: {
    executor: "constant-arrival-rate",
    rate: SENDS_PER_MIN,
    timeUnit: "1m",
    duration: DURATION,
    preAllocatedVUs: 4,
    maxVUs: 10,
    exec: "operatorSend",
    tags: { scenario: "operators" },
  },
};

const operatorScenarios = {
  ws_operators: scenarioDefs.ws_operators,
  operator_reads: scenarioDefs.operator_reads,
  operator_sends: scenarioDefs.operator_sends,
};

export const options = {
  scenarios:
    SCENARIOS === "all"
      ? scenarioDefs
      : SCENARIOS === "operators"
        ? operatorScenarios
        : { incoming: scenarioDefs.incoming },
  thresholds: {
    "http_req_duration{scenario:incoming}": ["p(95)<200"], // ответ вебхука
    "http_req_duration{scenario:operators}": ["p(95)<500"], // REST оператора
    e2e_delivery_ms: ["p(95)<2000", "p(99)<5000"],
    "http_req_failed{scenario:operators}": ["rate<0.001"],
    "http_req_failed{scenario:incoming}": ["rate<0.001"],
  },
  noConnectionReuse: false,
  summaryTrendStats: ["avg", "min", "med", "p(90)", "p(95)", "p(99)", "max"],
};

// ------------------------------------------------------------ утилиты -------

function uuidv4() {
  return "xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx".replace(/[xy]/g, (c) => {
    const r = (Math.random() * 16) | 0;
    const v = c === "x" ? r : (r & 0x3) | 0x8;
    return v.toString(16);
  });
}

function auth(token, scenario) {
  return {
    headers: { Authorization: `Bearer ${token}`, "Content-Type": "application/json" },
    tags: { scenario: scenario || "operators" },
  };
}

function doLogin(user, scenario) {
  for (let attempt = 0; attempt < 4; attempt++) {
    const r = http.post(
      `${BASE}/api/v1/auth/login`,
      JSON.stringify({ email: user.email, password: user.password, remember: true }),
      { headers: { "Content-Type": "application/json" }, tags: { scenario: scenario || "auth" } },
    );
    loginTime.add(r.timings.duration);
    if (r.status === 200) return r.json("access_token");
    loginFail.add(1);
    sleep(6 + Math.random() * 6); // nginx: login 10 r/m на IP
  }
  return null;
}

/** Конверт вебхука Авито Messenger v3 — контракт fake_avito/main.py:_webhook_payload. */
function makeAvitoPayload(chatIdx, sentAt, msgId) {
  const created = Math.floor(sentAt / 1000);
  return {
    id: uuidv4(),
    version: "v3.0.0",
    timestamp: created,
    payload: {
      type: "message",
      value: {
        id: msgId,
        chat_id: `u2i-${MARK}-${chatIdx}`,
        user_id: AVITO_USER_ID,
        author_id: 900000000 + chatIdx, // != avito_user_id, иначе эхо-отброс
        created,
        type: "text",
        chat_type: "u2i",
        content: { text: `${MARK}-K6|${sentAt}|${msgId}` },
        published_at: new Date(created * 1000).toISOString().replace(/\.\d+Z$/, "Z"),
        item_id: 3060161080,
        item: {
          id: 3060161080,
          title: `${MARK} лот`,
          price_string: "1 ₽",
          url: "https://avito.ru/items/3060161080",
        },
      },
    },
  };
}

// ------------------------------------------------------------- setup --------

export function setup() {
  if (OP_USERS.length === 0) throw new Error("OP_USERS пуст (email:password:send|read)");
  const tokens = OP_USERS.map((u) => {
    const token = doLogin(u, "auth");
    if (!token) throw new Error(`не удалось залогинить ${u.email}`);
    sleep(1);
    return { email: u.email, canSend: u.canSend, token };
  });
  return { tokens };
}

// ------------------------------------------------- сценарий 1: вебхуки ------

export function incomingWebhook() {
  const chatIdx = Math.floor(Math.random() * CHAT_POOL);
  const sentAt = Date.now();
  const msgId = `${MARK}-${uuidv4()}`;

  let r;
  if (INJECT_MODE === "fake") {
    r = http.post(
      `${FAKE_AVITO_URL}/_control/incoming`,
      JSON.stringify({
        account_user_id: AVITO_USER_ID,
        author: `${MARK} Клиент ${chatIdx}`,
        author_id: 900000000 + chatIdx,
        text: `${MARK}-K6|${sentAt}|${msgId}`,
        item: { id: 3060161080, title: `${MARK} лот`, price_string: "1 ₽" },
      }),
      { headers: { "Content-Type": "application/json" }, tags: { scenario: "incoming" } },
    );
  } else {
    r = http.post(
      `${BASE}/api/hooks/avito/${ACCOUNT_ID}?secret=${HOOK_SECRET}`,
      JSON.stringify(makeAvitoPayload(chatIdx, sentAt, msgId)),
      { headers: { "Content-Type": "application/json" }, tags: { scenario: "incoming" } },
    );
  }
  hookOk.add(check(r, { "hook 200": (res) => res.status === 200 }));
}

// -------------------------------------- сценарий 2: сокеты операторов -------

export function wsOperator(data) {
  const idx = (__VU - 1) % OPERATORS;
  const acc = data.tokens[idx % data.tokens.length];
  sleep(idx * STAGGER_SECONDS); // ступенчатый вход: nginx login zone 10 r/m

  let token = acc.token;
  let r = http.post(`${BASE}/api/v1/ws/ticket`, null, auth(token, "ws"));
  if (r.status === 401) {
    token = doLogin({ email: acc.email, password: passwordFor(acc.email) }, "ws") || token;
    relogins.add(1);
    r = http.post(`${BASE}/api/v1/ws/ticket`, null, auth(token, "ws"));
  }
  if (r.status !== 200) {
    wsHandshakeFail.add(1);
    sleep(10);
    return;
  }
  const ticket = r.json("ticket");

  const res = ws.connect(`${WS_BASE}/api/v1/ws?ticket=${ticket}`, {}, (socket) => {
    let closedByUs = false;
    socket.setInterval(() => socket.send(JSON.stringify({ type: "ping" })), 25000);
    socket.setTimeout(() => {
      closedByUs = true;
      socket.close();
    }, SESSION_MS);

    socket.on("message", (raw) => {
      let ev;
      try {
        ev = JSON.parse(raw);
      } catch (e) {
        return;
      }
      if (ev.type !== "message:new") return;
      wsMessages.add(1);
      const body = ((ev.data || {}).message || {}).body || "";
      const parts = String(body).split("|");
      if (parts.length < 2) return;
      const t0 = Number(parts[1]);
      if (!t0) return;
      if (parts[0] === `${MARK}-K6`) {
        e2eDelivery.add(Date.now() - t0); // одни часы: вброс и приём в k6
      } else if (parts[0] === `${MARK}-SRV`) {
        e2eServerInjected.add(Date.now() - (t0 - SKEW_MS));
      }
    });

    socket.on("close", () => {
      if (!closedByUs) wsUnexpectedClose.add(1);
    });
  });

  if (check(res, { "ws handshake 101": (x) => x && x.status === 101 })) wsSessions.add(1);
  else wsHandshakeFail.add(1);
}

// пароли в VU-контекст из setup не уезжают — держим отдельную карту
function passwordFor(email) {
  const u = OP_USERS.find((x) => x.email === email);
  return u ? u.password : "";
}

// ------------------------------- сценарий 3: чтение списков и лент ----------

function pickAccount(data, needSend) {
  const pool = data.tokens.filter((t) => (needSend ? t.canSend : true));
  return pool[Math.floor(Math.random() * pool.length)];
}

/** Токен VU: из setup; при 401 (TTL access-токена 900 с) — свой перелогин. */
const vuTokens = {};
function tokenOf(acc) {
  return vuTokens[acc.email] || acc.token;
}
function withAuth(acc, fn) {
  let r = fn(tokenOf(acc));
  if (r && r.status === 401) {
    const t = doLogin({ email: acc.email, password: passwordFor(acc.email) }, "operators");
    if (t) {
      vuTokens[acc.email] = t;
      relogins.add(1);
      r = fn(t);
    }
  }
  return r;
}

function markedConversations(acc) {
  const r = withAuth(acc, (token) =>
    http.get(`${BASE}/api/v1/conversations?tab=all&limit=50`, auth(token)),
  );
  if (!r || r.status !== 200) return [];
  return (r.json("items") || []).filter(
    (c) => c.last_message && String(c.last_message.body || "").startsWith(MARK),
  );
}

export function operatorRead(data) {
  const acc = pickAccount(data, false);
  // 2/3 итераций — список, 1/3 — лента конкретного диалога
  if (Math.random() < 0.66) {
    const tab = Math.random() < 0.5 ? "mine" : "all";
    withAuth(acc, (token) =>
      http.get(`${BASE}/api/v1/conversations?tab=${tab}&limit=50`, auth(token)),
    );
    return;
  }
  const items = markedConversations(acc);
  if (items.length === 0) return;
  const c = items[Math.floor(Math.random() * items.length)];
  withAuth(acc, (token) =>
    http.get(`${BASE}/api/v1/conversations/${c.id}/messages?limit=50`, auth(token)),
  );
}

// --------------------------------- сценарий 4: ответы операторов ------------

export function operatorSend(data) {
  const acc = pickAccount(data, true);
  if (!acc) return;
  const items = markedConversations(acc);
  if (items.length === 0) return;
  const c = items[Math.floor(Math.random() * items.length)];
  const r = withAuth(acc, (token) =>
    http.post(
      `${BASE}/api/v1/conversations/${c.id}/messages`,
      JSON.stringify({
        text: `${MARK}-OUT|${Date.now()}| ответ оператора: уточняю по вашему вопросу.`,
        client_message_id: uuidv4(), // 01 §1.6
      }),
      auth(token),
    ),
  );
  if (r && (r.status === 201 || r.status === 200)) opSent.add(1);
  else opSendFail.add(1);
}

// ------------------------------------------------------------- сводка -------

export function handleSummary(data) {
  const m = data.metrics;
  const num = (v) => (v === undefined || v === null ? "—" : Math.round(v * 100) / 100);
  const trend = (name, label) => {
    const t = m[name];
    if (!t || !t.values || t.values.count === 0) return `${label || name}: нет данных`;
    const v = t.values;
    return `${label || name}: med=${num(v.med)} p90=${num(v["p(90)"])} p95=${num(
      v["p(95)"],
    )} p99=${num(v["p(99)"])} max=${num(v.max)}`;
  };
  const counter = (name) => `${name}: ${m[name] ? num(m[name].values.count) : 0}`;
  const failed = m["http_req_failed"] ? m["http_req_failed"].values : null;
  const lines = [
    "",
    `=== LeadChat load (07 §3) run=${RUN} scenarios=${SCENARIOS} inject=${INJECT_MODE} ===`,
    trend("http_req_duration{scenario:incoming}", "вебхук, мс"),
    trend("http_req_duration{scenario:operators}", "REST оператора, мс"),
    trend("login_ms", "логин, мс"),
    trend("e2e_delivery_ms", "e2e вебхук->WS (k6), мс"),
    trend("e2e_delivery_srv_ms", "e2e вброс с сервера->WS, мс"),
    failed
      ? `неуспешных HTTP: ${failed.passes} из ${failed.passes + failed.fails} (${num(
          failed.rate * 100,
        )}%)`
      : "неуспешных HTTP: —",
    counter("op_ws_sessions"),
    counter("ws_messages_received"),
    counter("ws_unexpected_close"),
    counter("ws_handshake_failures"),
    counter("operator_messages_sent"),
    counter("operator_messages_failed"),
    counter("login_failures"),
    counter("relogins"),
    "",
  ];
  return {
    stdout: lines.join("\n"),
    "summary.json": JSON.stringify(data, null, 2),
  };
}

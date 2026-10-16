const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

let menuItems = [];
let selectedCategory = "";
let activeFlavor = "";
let reservationDraft = null;
let menuSearchUsesMilvus = false;
let menuSearchRequest = 0;
let menuSearchTimer = null;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  })[char]);
}

async function api(path, options = {}) {
  const response = await fetch(path, {
    ...options,
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
  });
  let body;
  try { body = await response.json(); } catch { body = {}; }
  if (!response.ok) {
    const detail = body.detail;
    const message = typeof detail === "string" ? detail : detail?.message || body.message || `请求失败（${response.status}）`;
    throw new Error(message);
  }
  return body;
}

function menuCard(item) {
  const tags = (item.flavors || []).map((flavor) => `<span>${escapeHtml(flavor)}</span>`).join("");
  const availability = item.is_available ? "可供应" : "暂时售罄";
  const allergens = item.allergens ? `<small class="dish-allergens">过敏原：${escapeHtml(item.allergens)}</small>` : "";
  return `<article class="menu-card">
    <div class="menu-card-top"><span class="dish-category">${escapeHtml(item.category)}${item.is_featured ? " · 特色" : ""}</span><span class="availability ${item.is_available ? "" : "sold-out"}">${availability}</span></div>
    <h3>${escapeHtml(item.dish_name)}</h3><p>${escapeHtml(item.description)}</p>
    <div class="dish-tags">${tags}</div>${allergens}<div class="dish-price"><small>¥</small>${Number(item.price).toFixed(0)}</div>
  </article>`;
}

function renderMenu() {
  const keyword = $("#menuSearch").value.trim().toLocaleLowerCase();
  const filtered = menuItems.filter((item) => {
    const categoryMatch = !selectedCategory || item.category === selectedCategory;
    const searchText = `${item.dish_name} ${item.category} ${item.description} ${(item.flavors || []).join(" ")}`.toLocaleLowerCase();
    return categoryMatch && (!keyword || menuSearchUsesMilvus || searchText.includes(keyword));
  });
  $("#menuGrid").innerHTML = filtered.length
    ? filtered.map(menuCard).join("")
    : '<div class="empty-card">没有找到符合条件的菜品，换个关键词试试。</div>';
}

async function loadMenu() {
  try {
    const data = await api("/api/menu");
    menuItems = data.items || [];
    renderMenu();
  } catch (error) {
    $("#menuGrid").innerHTML = `<div class="empty-card">菜单暂时无法加载：${escapeHtml(error.message)}</div>`;
  }
}

function renderRecommendations(flavor, items) {
  const root = $("#tasteResult");
  if (!items.length) {
    root.innerHTML = `<div class="result-empty"><span class="result-spark">✳</span><strong>暂时没有匹配的菜品</strong><small>试试其他口味吧</small></div>`;
    return;
  }
  root.innerHTML = `<h3>为你挑了几道 · ${escapeHtml(flavor)}</h3><div class="recommendation-list">${items.slice(0, 4).map((item) => `
    <div class="recommendation-item"><div><strong>${escapeHtml(item.dish_name)}</strong><small>${escapeHtml(item.description)}</small></div><em>¥${Number(item.price).toFixed(0)}</em></div>`).join("")}</div>`;
}

async function loadFlavor(flavor, button) {
  activeFlavor = flavor;
  $$("#tasteOptions button").forEach((node) => node.classList.toggle("active", node === button));
  $("#tasteResult").innerHTML = '<div class="result-empty"><span class="result-spark">✳</span><strong>正在找合口味的菜</strong><small>请稍等一下</small></div>';
  try {
    const data = await api(`/api/recommendations/flavor?flavor=${encodeURIComponent(flavor)}`);
    renderRecommendations(flavor, data.items || []);
  } catch (error) {
    $("#tasteResult").innerHTML = `<div class="result-empty"><strong>暂时无法获取推荐</strong><small>${escapeHtml(error.message)}</small></div>`;
  }
}

function setShanghaiTimeLimits() {
  const formatter = new Intl.DateTimeFormat("sv-SE", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
    hour: "2-digit", minute: "2-digit", hourCycle: "h23",
  });
  const shanghaiNow = formatter.format(new Date()).replace(" ", "T");
  const field = $("[name='arrival_time']");
  field.min = shanghaiNow;
  const nextDay = new Date(Date.now() + 24 * 60 * 60 * 1000);
  const date = new Intl.DateTimeFormat("sv-SE", { timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit" }).format(nextDay);
  field.value = `${date}T18:30`;
}

function formDataToReservation(form) {
  const data = Object.fromEntries(new FormData(form).entries());
  return {
    customer_name: data.customer_name.trim(),
    phone: data.phone.trim(),
    num_people: Number(data.num_people),
    num_children: Number(data.num_children || 0),
    arrival_time: data.arrival_time,
    seat_preference: data.seat_preference,
    main_dish_preference: data.main_dish_preference.trim(),
    other_comments: (data.other_comments || "").trim(),
  };
}

function showReservationConfirmation(details) {
  const dateText = details.arrival_time.replace("T", " ");
  $("#reservationSummary").textContent = `${details.customer_name}，联系电话 ${details.phone}。${dateText} 到店，共 ${details.num_people} 人，其中儿童 ${details.num_children} 人。座位偏好：${details.seat_preference}。主菜偏好：${details.main_dish_preference}。其他备注：${details.other_comments || "无"}。`;
  $("#reservationForm").classList.add("hidden");
  $("#confirmationPanel").classList.remove("hidden");
  $("#reservationMessage").textContent = "";
  $("#reservationMessage").classList.remove("error");
}

function resetReservationConfirmation() {
  $("#reservationForm").classList.remove("hidden");
  $("#confirmationPanel").classList.add("hidden");
}

function renderReservations(items) {
  const root = $("#reservationList");
  if (!items.length) {
    root.innerHTML = '<div class="empty-card">暂时没有找到预订记录。</div>';
    return;
  }
  root.innerHTML = items.map((item) => `<article class="reservation-entry">
    <strong>${escapeHtml(item.reservation_id)} · ${escapeHtml(item.customer_name)}</strong>
    <div>${escapeHtml(String(item.arrival_time).replace("T", " "))} · ${escapeHtml(item.num_people)} 人（儿童 ${escapeHtml(item.num_children)} 人）</div>
    <small>${escapeHtml(item.seat_preference)} · 主菜偏好：${escapeHtml(item.main_dish_preference)} · ${escapeHtml(item.status)}</small>
  </article>`).join("");
}

function appendMessage(role, text) {
  const row = document.createElement("div");
  row.className = `message-row ${role === "user" ? "user-row" : "assistant-row"}`;
  if (role !== "user") {
    const avatar = document.createElement("span");
    avatar.className = "mini-avatar";
    avatar.textContent = "六";
    row.append(avatar);
  }
  const bubble = document.createElement("div");
  bubble.className = "message-bubble";
  bubble.textContent = text;
  row.append(bubble);
  const messages = $("#chatMessages");
  messages.append(row);
  messages.scrollTop = messages.scrollHeight;
}

async function sendChatMessage(message) {
  const text = message.trim();
  if (!text) return;
  appendMessage("user", text);
  $("#chatInput").value = "";
  const sessionId = localStorage.getItem("liu_he_chat_session") || crypto.randomUUID();
  localStorage.setItem("liu_he_chat_session", sessionId);
  const prompts = $("#quickPrompts");
  prompts.classList.add("hidden");
  try {
    const data = await api("/api/chat", { method: "POST", body: JSON.stringify({ session_id: sessionId, message: text }) });
    appendMessage("assistant", data.answer);
  } catch (error) {
    appendMessage("assistant", `刚才没有连上服务：${error.message}。请稍后再试。`);
  }
}

function setupMenu() {
  $("#menuFilters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-category]");
    if (!button) return;
    selectedCategory = button.dataset.category;
    $$("#menuFilters button").forEach((node) => node.classList.toggle("active", node === button));
    renderMenu();
  });
  $("#menuSearch").addEventListener("input", () => {
    const query = $("#menuSearch").value.trim();
    const requestId = ++menuSearchRequest;
    clearTimeout(menuSearchTimer);
    if (!query) {
      menuSearchUsesMilvus = false;
      loadMenu();
      return;
    }
    menuSearchTimer = setTimeout(async () => {
      $("#menuGrid").innerHTML = '<div class="loading-card">正在用 Milvus 检索菜单…</div>';
      try {
        const data = await api(`/api/search/menu?query=${encodeURIComponent(query)}&limit=10`);
        if (requestId !== menuSearchRequest || query !== $("#menuSearch").value.trim()) return;
        menuItems = data.items || [];
        menuSearchUsesMilvus = true;
        renderMenu();
      } catch (error) {
        if (requestId !== menuSearchRequest) return;
        $("#menuGrid").innerHTML = `<div class="empty-card">菜单语义检索暂时不可用：${escapeHtml(error.message)}</div>`;
      }
    }, 220);
  });
}

function setupBooking() {
  const form = $("#reservationForm");
  const childrenField = form.elements.namedItem("num_children");
  childrenField.addEventListener("change", () => childrenField.setCustomValidity(""));
  form.addEventListener("submit", (event) => {
    event.preventDefault();
    childrenField.setCustomValidity("");
    if (!form.reportValidity()) return;
    reservationDraft = formDataToReservation(form);
    if (reservationDraft.num_children > reservationDraft.num_people) {
      childrenField.setCustomValidity("儿童人数不能大于就餐总人数。");
      childrenField.reportValidity();
      return;
    }
    showReservationConfirmation(reservationDraft);
  });
  $("#editReservation").addEventListener("click", resetReservationConfirmation);
  $("#confirmReservation").addEventListener("click", async (event) => {
    if (!reservationDraft) return;
    const button = event.currentTarget;
    const message = $("#reservationMessage");
    message.textContent = "正在登记预订…";
    message.classList.remove("error");
    button.disabled = true;
    let requestId = localStorage.getItem("liu_he_pending_reservation_request");
    const previousPayload = localStorage.getItem("liu_he_pending_reservation_payload");
    const currentPayload = JSON.stringify(reservationDraft);
    if (!requestId || previousPayload !== currentPayload) {
      requestId = crypto.randomUUID();
      localStorage.setItem("liu_he_pending_reservation_request", requestId);
      localStorage.setItem("liu_he_pending_reservation_payload", currentPayload);
    }
    try {
      const data = await api("/api/reservations", {
        method: "POST",
        body: JSON.stringify({ ...reservationDraft, confirmed: true, request_id: requestId, thread_id: `web:${reservationDraft.phone}` }),
      });
      const item = data.reservation;
      message.textContent = `预订已确认，编号 ${item.reservation_id}。请记下编号以便查询。`;
      localStorage.removeItem("liu_he_pending_reservation_request");
      localStorage.removeItem("liu_he_pending_reservation_payload");
      form.reset();
      reservationDraft = null;
      setShanghaiTimeLimits();
      await lookupReservations(item.phone);
    } catch (error) {
      message.textContent = error.message;
      message.classList.add("error");
    } finally {
      button.disabled = false;
    }
  });
}

async function lookupReservations(phone) {
  const list = $("#reservationList");
  list.innerHTML = '<div class="loading-card">正在查询预订记录…</div>';
  try {
    const data = await api(`/api/reservations?phone=${encodeURIComponent(phone)}&limit=20`);
    renderReservations(data.items || []);
  } catch (error) {
    list.innerHTML = `<div class="empty-card">查询失败：${escapeHtml(error.message)}</div>`;
  }
}

function setupLookup() {
  $("#lookupForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!event.currentTarget.reportValidity()) return;
    await lookupReservations($("#lookupPhone").value.trim());
  });
}

function setupDelivery() {
  $("#deliveryForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    if (!form.reportValidity()) return;
    const button = $("button[type='submit']", form);
    const resultBox = $("#deliveryResult");
    const data = Object.fromEntries(new FormData(form).entries());
    button.disabled = true;
    button.innerHTML = "正在查询 <span>…</span>";
    resultBox.className = "delivery-result";
    resultBox.textContent = "正在调用配送路线查询…";
    resultBox.classList.remove("hidden");
    try {
      const result = await api("/api/delivery", { method: "POST", body: JSON.stringify(data) });
      if (!result.success) {
        resultBox.className = "delivery-result error";
        resultBox.textContent = result.message;
      } else {
        resultBox.className = `delivery-result ${result.in_range ? "" : "out-of-range"}`;
        const duration = result.duration_min == null ? "" : ` · 预计 ${result.duration_min} 分钟`;
        resultBox.innerHTML = `<strong>${result.in_range ? "在配送范围内" : "超出配送范围"}</strong>${escapeHtml(result.address)} · ${escapeHtml(result.travel_mode)}约 ${Number(result.distance_km).toFixed(1)} 公里${duration}。`;
      }
    } catch (error) {
      resultBox.className = "delivery-result error";
      resultBox.textContent = error.message;
    } finally {
      button.disabled = false;
      button.innerHTML = "查询配送 <span>→</span>";
    }
  });
}

function setupChat() {
  $("#chatForm").addEventListener("submit", (event) => {
    event.preventDefault();
    sendChatMessage($("#chatInput").value);
  });
  $("#quickPrompts").addEventListener("click", (event) => {
    const button = event.target.closest("button");
    if (button) sendChatMessage(button.textContent);
  });
}

document.addEventListener("DOMContentLoaded", () => {
  setupMenu();
  setupBooking();
  setupLookup();
  setupDelivery();
  setupChat();
  setShanghaiTimeLimits();
  loadMenu();
  api("/api/health").then((health) => {
    const source = {
      amap: "地图来源：高德实时路线",
      demo: "地图来源：本地演示数据",
      "amap-unconfigured": "高德地图 Key 尚未配置",
    };
    $("#mapProviderNotice").textContent = source[health.maps_provider] || "地图服务配置无效";
  }).catch(() => {});
  $$("#tasteOptions button").forEach((button) => button.addEventListener("click", () => loadFlavor(button.dataset.flavor, button)));
});


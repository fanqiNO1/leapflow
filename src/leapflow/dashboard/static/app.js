// LeapBoard: a minimal Server-Driven UI renderer.
// Fetches a ViewSpec from /api/view, renders the fixed component catalog into
// the DOM, connects a WebSocket for live monitor events, and posts interactive
// actions back to /api/action.
(function () {
  "use strict";

  const params = new URLSearchParams(location.search);
  const TOKEN = params.get("token") || "";
  const rootEl = document.getElementById("root");
  const statusEl = document.getElementById("status");
  const toastsEl = document.getElementById("toasts");
  const localeEl = document.getElementById("locale-switch");
  const storedLocale = localStorage.getItem("leapboard.locale") || "";
  const browserLocale = (navigator.language || "en").slice(0, 2).toLowerCase();
  let locale = storedLocale || (["en", "zh", "fr", "es", "ar", "ru"].includes(browserLocale) ? browserLocale : "en");
  let current = { template: params.get("template") || "" };
  const HIDDEN_NAV_TEMPLATES = new Set(["finance", "research", "sentiment"]);
  let figSeq = 0;  // academic figure counter, reset each render()
  let tblSeq = 0;  // academic table counter, reset each render()

  // ── Signal auto-refresh state ──
  let _signalRefreshTimer = null;
  let _signalEventCount = 0;

  function getCurrentTemplate() { return current.template || ""; }

  function startSignalAutoRefresh() {
    stopSignalAutoRefresh();
    _signalRefreshTimer = setInterval(function () {
      if (getCurrentTemplate() === "signals") fetchView();
    }, 5000);
  }

  function stopSignalAutoRefresh() {
    if (_signalRefreshTimer) { clearInterval(_signalRefreshTimer); _signalRefreshTimer = null; }
  }

  function incrementSignalCounter() {
    _signalEventCount++;
    var counterEl = document.getElementById("signal-event-counter");
    if (counterEl) counterEl.textContent = String(_signalEventCount);
  }

  function injectSignalRefreshBtn() {
    if (getCurrentTemplate() !== "signals") return;
    // Find the page title or first section title to attach the controls.
    var title = rootEl.querySelector(".page-title") || rootEl.querySelector(".section-title");
    if (!title) return;
    // Avoid duplicates; controls belong to the title node, never the page
    // container, so the page keeps its vertical document flow.
    var header = title;
    if (header.querySelector(".refresh-btn")) return;
    header.classList.add("with-actions");
    var btn = document.createElement("button");
    btn.className = "refresh-btn";
    btn.textContent = "\u21bb";  // ↻
    btn.title = "Refresh signal metrics";
    btn.addEventListener("click", function () {
      btn.disabled = true;
      btn.classList.add("refreshing");
      fetchView().then(function () {
        setTimeout(function () { btn.disabled = false; btn.classList.remove("refreshing"); }, 400);
      }).catch(function () {
        btn.disabled = false; btn.classList.remove("refreshing");
      });
    });
    header.appendChild(btn);
    // Also inject event counter badge next to the button
    var counter = document.createElement("span");
    counter.className = "signal-counter-badge";
    counter.id = "signal-event-counter";
    counter.textContent = String(_signalEventCount);
    counter.title = "WebSocket events received";
    header.appendChild(counter);
  }

  const I18N = {
    en: { "manual_refresh": "manual refresh", "first_observation": "first observation", "artifact_changed": "artifact changed", "batch_turns": "turn threshold", "batch_tokens": "token threshold", "model_salience": "model salience", "text_only": "conversation text", "text_and_artifacts": "conversation + files", "partial_artifacts": "partial files" },
    zh: { "Overview": "概览", "Session": "会话", "Language": "语言", "connecting…": "连接中…", "live": "实时", "reconnecting…": "重连中…", "Loading…": "加载中…", "No content yet.": "暂无内容。", "Failed to load view": "视图加载失败", "Action failed": "操作失败", "Candlestick": "K线", "Series": "序列", "Gauge": "仪表", "Custom": "自定义", "unknown": "未知", "Watch portfolio": "观察组合", "Refresh cadence": "刷新节奏", "Active watches": "活跃观察", "Recent findings": "最新发现", "Watches": "观察任务", "Findings": "发现", "Signals": "信号", "Watch": "观察", "Price action": "价格行为", "Signal mix": "信号结构", "Market brief": "市场简报", "Latest sentiment": "最新情绪", "Mentions": "提及", "Sentiment structure": "情绪结构", "Narrative pulse": "叙事脉搏", "New papers": "新论文", "Research pipeline": "研究管线", "Evidence stream": "证据流", "Executive brief": "执行摘要", "Storyline": "叙事线", "Insights": "洞察", "Action items": "行动项", "Decisions": "决策", "Open questions": "待回答问题", "Entities": "实体", "Suggested next prompts": "建议追问", "Timeline": "时间线", "Severity mix": "严重度结构", "alert": "警报", "notable": "重要", "info": "信息", "Observation status": "观察状态", "Refresh state": "刷新状态", "Refresh reason": "刷新原因", "Coverage": "覆盖率", "Artifacts": "副产物", "Observed context": "已观察上下文", "File artifacts": "文件副产物", "File": "文件", "Status": "状态", "Note": "说明", "manual_refresh": "手动刷新", "first_observation": "首次观察", "artifact_changed": "文件副产物变化", "batch_turns": "轮次阈值", "batch_tokens": "上下文阈值", "model_salience": "模型显著性", "Session Analysis": "会话分析", "Observation": "观察", "Operating agenda": "行动议程", "Context map": "上下文图谱", "Next prompts": "后续追问", "Turns": "轮次", "Tokens": "词元", "Reason": "原因", "Abstract": "摘要", "No entries.": "暂无条目。", "Insight count by severity.": "按严重度统计的洞察数。", "Session file artifacts.": "会话文件副产物。", "Coverage · storyline · severity": "覆盖率 · 叙事 · 严重度", "Trigger and context": "触发与上下文", "Key observations": "关键观察", "Decisions and actions": "决策与行动", "Entities and follow-ups": "实体与后续", "Extracted from this session's tool/file output (not model-generated).": "数据来自本次会话的工具/文件产物（非模型生成）。", "Signal flow": "信号流", "Subscribers": "订阅者", "Active triggers": "活跃触发器", "Buffer dropped": "缓冲丢弃", "Debounced": "去抖", "Live signal stream": "实时信号流", "Recent events (last 50)": "最近事件（最新50条）", "Active event-driven monitors": "活跃的事件驱动监视器", "Name": "名称", "Domain": "领域", "Trigger": "触发器", "Latest observation results": "最新观测结果" },
    fr: { "Overview": "Vue d’ensemble", "Session": "Session", "Language": "Langue", "connecting…": "connexion…", "live": "direct", "reconnecting…": "reconnexion…", "Loading…": "chargement…", "No content yet.": "Aucun contenu.", "Failed to load view": "Échec du chargement", "Action failed": "Action échouée", "Watch": "Veille", "Watches": "Veilles", "Findings": "Constats", "Recent findings": "Constats récents", "Signals": "Signaux", "Insights": "Analyses", "Action items": "Actions", "Decisions": "Décisions", "Open questions": "Questions ouvertes", "Entities": "Entités", "Suggested next prompts": "Prochaines invites", "Executive brief": "Synthèse exécutive", "Storyline": "Narratif", "Timeline": "Chronologie", "Severity mix": "Mix de sévérité", "alert": "alerte", "notable": "notable", "info": "info", "Observation status": "Statut d’observation", "Refresh state": "État", "Refresh reason": "Raison", "Coverage": "Couverture", "Artifacts": "Artefacts", "Observed context": "Contexte observé", "File artifacts": "Fichiers", "File": "Fichier", "Status": "Statut", "Note": "Note", "manual_refresh": "actualisation manuelle", "first_observation": "première observation", "artifact_changed": "artefact modifié", "batch_turns": "seuil de tours", "batch_tokens": "seuil de jetons", "model_salience": "saillance modèle", "Session Analysis": "Analyse de session", "Observation": "Observation", "Operating agenda": "Programme d’action", "Context map": "Carte de contexte", "Next prompts": "Invites suivantes", "Turns": "Tours", "Tokens": "Jetons", "Reason": "Raison", "Abstract": "Résumé", "No entries.": "Aucune entrée.", "Insight count by severity.": "Nombre d’analyses par sévérité.", "Session file artifacts.": "Artefacts de fichiers de session.", "Coverage · storyline · severity": "Couverture · récit · sévérité", "Trigger and context": "Déclencheur et contexte", "Key observations": "Observations clés", "Decisions and actions": "Décisions et actions", "Entities and follow-ups": "Entités et suivis", "Signal flow": "Flux de signaux", "Subscribers": "Abonnés", "Active triggers": "Déclencheurs actifs", "Buffer dropped": "Tampon perdu", "Debounced": "Antirebond", "Live signal stream": "Flux de signaux en direct", "Recent events (last 50)": "Événements récents (50 derniers)", "Active event-driven monitors": "Moniteurs événementiels actifs", "Name": "Nom", "Domain": "Domaine", "Trigger": "Déclencheur", "Latest observation results": "Derniers résultats d'observation" },
    es: { "Overview": "Resumen", "Session": "Sesión", "Language": "Idioma", "connecting…": "conectando…", "live": "en vivo", "reconnecting…": "reconectando…", "Loading…": "cargando…", "No content yet.": "Sin contenido.", "Failed to load view": "Error al cargar", "Action failed": "Acción fallida", "Watch": "Vigilancia", "Watches": "Vigilancias", "Findings": "Hallazgos", "Recent findings": "Hallazgos recientes", "Signals": "Señales", "Insights": "Ideas", "Action items": "Acciones", "Decisions": "Decisiones", "Open questions": "Preguntas abiertas", "Entities": "Entidades", "Suggested next prompts": "Siguientes preguntas", "Executive brief": "Resumen ejecutivo", "Storyline": "Narrativa", "Timeline": "Cronología", "Severity mix": "Mezcla de severidad", "alert": "alerta", "notable": "relevante", "info": "info", "Observation status": "Estado de observación", "Refresh state": "Estado", "Refresh reason": "Motivo", "Coverage": "Cobertura", "Artifacts": "Artefactos", "Observed context": "Contexto observado", "File artifacts": "Archivos", "File": "Archivo", "Status": "Estado", "Note": "Nota", "manual_refresh": "actualización manual", "first_observation": "primera observación", "artifact_changed": "artefacto cambiado", "batch_turns": "umbral de turnos", "batch_tokens": "umbral de tokens", "model_salience": "relevancia del modelo", "Session Analysis": "Análisis de sesión", "Observation": "Observación", "Operating agenda": "Agenda operativa", "Context map": "Mapa de contexto", "Next prompts": "Siguientes prompts", "Turns": "Turnos", "Tokens": "Tokens", "Reason": "Motivo", "Abstract": "Resumen", "No entries.": "Sin entradas.", "Insight count by severity.": "Recuento de hallazgos por severidad.", "Session file artifacts.": "Artefactos de archivos de sesión.", "Coverage · storyline · severity": "Cobertura · relato · severidad", "Trigger and context": "Disparador y contexto", "Key observations": "Observaciones clave", "Decisions and actions": "Decisiones y acciones", "Entities and follow-ups": "Entidades y seguimientos", "Signal flow": "Flujo de señales", "Subscribers": "Suscriptores", "Active triggers": "Disparadores activos", "Buffer dropped": "Buffer perdido", "Debounced": "Antirrebote", "Live signal stream": "Flujo de señales en vivo", "Recent events (last 50)": "Eventos recientes (últimos 50)", "Active event-driven monitors": "Monitores por eventos activos", "Name": "Nombre", "Domain": "Dominio", "Trigger": "Disparador", "Latest observation results": "Últimos resultados de observación" },
    ar: { "Overview": "نظرة عامة", "Session": "الجلسة", "Language": "اللغة", "connecting…": "جارٍ الاتصال…", "live": "مباشر", "reconnecting…": "إعادة الاتصال…", "Loading…": "جارٍ التحميل…", "No content yet.": "لا يوجد محتوى بعد.", "Failed to load view": "فشل تحميل العرض", "Action failed": "فشل الإجراء", "Watch": "مراقبة", "Watches": "المراقبات", "Findings": "النتائج", "Recent findings": "أحدث النتائج", "Signals": "الإشارات", "Insights": "الرؤى", "Action items": "إجراءات", "Decisions": "قرارات", "Open questions": "أسئلة مفتوحة", "Entities": "كيانات", "Suggested next prompts": "أسئلة مقترحة", "Executive brief": "ملخص تنفيذي", "Storyline": "السرد", "Timeline": "الخط الزمني", "Severity mix": "توزيع الشدة", "alert": "تنبيه", "notable": "مهم", "info": "معلومة", "Observation status": "حالة المراقبة", "Refresh state": "حالة التحديث", "Refresh reason": "سبب التحديث", "Coverage": "التغطية", "Artifacts": "المخرجات", "Observed context": "السياق المرصود", "File artifacts": "ملفات", "File": "ملف", "Status": "الحالة", "Note": "ملاحظة", "manual_refresh": "تحديث يدوي", "first_observation": "أول مراقبة", "artifact_changed": "تغير ملف", "batch_turns": "حد الجولات", "batch_tokens": "حد الرموز", "model_salience": "أهمية النموذج", "Session Analysis": "تحليل الجلسة", "Observation": "الرصد", "Operating agenda": "خطة العمل", "Context map": "خريطة السياق", "Next prompts": "المطالبات التالية", "Turns": "الأدوار", "Tokens": "الرموز", "Reason": "السبب", "Abstract": "ملخص", "No entries.": "لا توجد إدخالات.", "Insight count by severity.": "عدد الرؤى حسب الخطورة.", "Session file artifacts.": "مخرجات ملفات الجلسة.", "Coverage · storyline · severity": "التغطية · السرد · الخطورة", "Trigger and context": "المُشغِّل والسياق", "Key observations": "ملاحظات رئيسية", "Decisions and actions": "القرارات والإجراءات", "Entities and follow-ups": "الكيانات والمتابعات", "Signal flow": "تدفق الإشارات", "Subscribers": "المشتركون", "Active triggers": "المُشغِّلات النشطة", "Buffer dropped": "ذاكرة مؤقتة مُسقَطة", "Debounced": "مُزال الارتداد", "Live signal stream": "تدفق الإشارات المباشر", "Recent events (last 50)": "الأحداث الأخيرة (آخر 50)", "Active event-driven monitors": "مراقبات حدثية نشطة", "Name": "الاسم", "Domain": "المجال", "Trigger": "المُشغِّل", "Latest observation results": "أحدث نتائج الرصد" },
    ru: { "Overview": "Обзор", "Session": "Сессия", "Language": "Язык", "connecting…": "подключение…", "live": "онлайн", "reconnecting…": "переподключение…", "Loading…": "загрузка…", "No content yet.": "Пока нет данных.", "Failed to load view": "Не удалось загрузить", "Action failed": "Действие не выполнено", "Watch": "Наблюдение", "Watches": "Наблюдения", "Findings": "Находки", "Recent findings": "Последние находки", "Signals": "Сигналы", "Insights": "Инсайты", "Action items": "Действия", "Decisions": "Решения", "Open questions": "Открытые вопросы", "Entities": "Сущности", "Suggested next prompts": "Следующие запросы", "Executive brief": "Краткий обзор", "Storyline": "Сюжет", "Timeline": "Хронология", "Severity mix": "Структура важности", "alert": "тревога", "notable": "важно", "info": "инфо", "Observation status": "Статус наблюдения", "Refresh state": "Состояние", "Refresh reason": "Причина", "Coverage": "Покрытие", "Artifacts": "Артефакты", "Observed context": "Наблюдаемый контекст", "File artifacts": "Файлы", "File": "Файл", "Status": "Статус", "Note": "Заметка", "manual_refresh": "ручное обновление", "first_observation": "первое наблюдение", "artifact_changed": "файл изменён", "batch_turns": "порог ходов", "batch_tokens": "порог токенов", "model_salience": "значимость модели", "Session Analysis": "Анализ сессии", "Observation": "Наблюдение", "Operating agenda": "Рабочая повестка", "Context map": "Карта контекста", "Next prompts": "Следующие запросы", "Turns": "Ходы", "Tokens": "Токены", "Reason": "Причина", "Abstract": "Аннотация", "No entries.": "Нет записей.", "Insight count by severity.": "Число инсайтов по важности.", "Session file artifacts.": "Файловые артефакты сессии.", "Coverage · storyline · severity": "Покрытие · сюжет · важность", "Trigger and context": "Триггер и контекст", "Key observations": "Ключевые наблюдения", "Decisions and actions": "Решения и действия", "Entities and follow-ups": "Сущности и продолжения", "Signal flow": "Поток сигналов", "Subscribers": "Подписчики", "Active triggers": "Активные триггеры", "Buffer dropped": "Потери буфера", "Debounced": "Дебаунс", "Live signal stream": "Поток сигналов (live)", "Recent events (last 50)": "Последние события (50)", "Active event-driven monitors": "Активные событийные мониторы", "Name": "Имя", "Domain": "Домен", "Trigger": "Триггер", "Latest observation results": "Последние результаты наблюдений" }
  };

  const I18N_PATCH = {
    en: {
      "All": "All",
      "connecting…": "connecting",
      "live": "connected",
      "reconnecting…": "reconnecting",
      "seconds ago": "{count}s ago",
      "minutes ago": "{count}m ago",
      "hours ago": "{count}h ago",
      "Showing {shown} of {total} recent events.": "Showing {shown} of {total} recent events.",
      "Showing {shown} of {total} {family} events.": "Showing {shown} of {total} {family} events.",
      "stale build": "stale build",
      "stale_build_title": "This LeapBoard server (pid {pid}) predates the current source tree. Restart it to pick up recent changes.",
      "Stream events": "Stream events", "Active watches": "Active watches", "Watch portfolio": "Watch portfolio", "Noise suppressed": "Noise suppressed", "Source dropped": "Source dropped", "Reorder pending": "Reorder pending",
      "Signal health summary": "Signal health summary", "Ingress": "Ingress", "Pressure": "Pressure", "Recent event families": "Recent event families",
      "Finding severity mix": "Finding severity mix", "Watch state mix": "Watch state mix", "Watch states": "Watch states", "Trigger coverage": "Trigger coverage",
      "Latest daemon events · grouped by signal family · newest first.": "Latest daemon events · grouped by signal family · newest first.",
      "Ingress fan-out, pipeline pressure, and recent dimensional mix.": "Ingress fan-out, pipeline pressure, and recent dimensional mix.",
      "Event count by normalized family in the live ring buffer.": "Event count by normalized family in the live ring buffer.",
      "Observation count by severity across recent findings.": "Observation count by severity across recent findings.",
      "Current monitor lifecycle states.": "Current monitor lifecycle states.", "Active and completed event-driven monitors.": "Active and completed event-driven monitors.",
      "Latest observation results.": "Latest observation results.", "Event patterns registered with the monitor event bridge.": "Event patterns registered with the monitor event bridge.",
      "Triggers": "Triggers", "Watches": "Watches", "Pattern": "Pattern", "Triggered": "Triggered", "Last event": "Last event", "Value": "Value", "Dimension": "Dimension", "Signal": "Signal",
      "armed": "armed", "done": "done", "suspended": "suspended", "yes": "yes", "no": "no",
      "signal.family.fs": "fs", "signal.family.gateway": "gateway", "signal.family.ui": "ui", "signal.family.clipboard": "clipboard", "signal.family.app": "app", "signal.family.unknown": "unknown"
    },
    zh: {
      "All": "全部",
      "connecting…": "正在连接",
      "live": "已连接",
      "reconnecting…": "正在重连",
      "seconds ago": "{count}秒前", "minutes ago": "{count}分钟前", "hours ago": "{count}小时前",
      "Showing {shown} of {total} recent events.": "显示最近 {total} 个事件中的 {shown} 个。",
      "Showing {shown} of {total} {family} events.": "显示 {total} 个{family}事件中的 {shown} 个。",
      "stale build": "构建已过期", "stale_build_title": "LeapBoard 服务（pid {pid}）早于当前源码树启动。请重启以加载最近的更改。",
      "Stream events": "流事件", "Active watches": "活跃观察", "Watch portfolio": "观察组合", "Noise suppressed": "已压制噪声", "Source dropped": "源丢弃", "Reorder pending": "重排待处理",
      "Signal health summary": "信号健康摘要", "Ingress": "输入", "Pressure": "压力", "Recent event families": "最近事件类别",
      "Finding severity mix": "发现严重度分布", "Watch state mix": "观察状态分布", "Watch states": "观察状态", "Trigger coverage": "触发覆盖",
      "Latest daemon events · grouped by signal family · newest first.": "最新 daemon 事件 · 按信号类别分组 · 最新优先。",
      "Ingress fan-out, pipeline pressure, and recent dimensional mix.": "输入扇出、管线压力和最近维度分布。",
      "Event count by normalized family in the live ring buffer.": "实时环形缓冲区中按标准化类别统计的事件数。",
      "Observation count by severity across recent findings.": "最近发现中按严重度统计的观察数。",
      "Current monitor lifecycle states.": "当前监视器生命周期状态。", "Active and completed event-driven monitors.": "活跃和已完成的事件驱动监视器。",
      "Latest observation results.": "最新观察结果。", "Event patterns registered with the monitor event bridge.": "监视器事件桥注册的事件模式。",
      "Triggers": "触发器", "Watches": "观察任务", "Pattern": "模式", "Triggered": "已触发", "Last event": "最后事件", "Value": "值", "Dimension": "维度", "Signal": "信号",
      "armed": "已布防", "done": "完成", "suspended": "已暂停", "yes": "是", "no": "否",
      "signal.family.fs": "文件", "signal.family.gateway": "网关", "signal.family.ui": "界面", "signal.family.clipboard": "剪贴板", "signal.family.app": "应用", "signal.family.unknown": "未知"
    },
    fr: {
      "All": "Tout", "connecting…": "connexion", "live": "connecté", "reconnecting…": "reconnexion", "seconds ago": "il y a {count} s", "minutes ago": "il y a {count} min", "hours ago": "il y a {count} h",
      "Showing {shown} of {total} recent events.": "Affichage de {shown} sur {total} événements récents.", "Showing {shown} of {total} {family} events.": "Affichage de {shown} sur {total} événements {family}.",
      "stale build": "build obsolète", "stale_build_title": "Ce serveur LeapBoard (pid {pid}) est antérieur à l'arbre source actuel. Redémarrez-le pour charger les changements récents.",
      "Stream events": "Événements de flux", "Active watches": "Veilles actives", "Watch portfolio": "Portefeuille de veilles", "Noise suppressed": "Bruit supprimé", "Source dropped": "Source rejetée", "Reorder pending": "Réordonnancement en attente",
      "Signal health summary": "Résumé santé des signaux", "Ingress": "Entrée", "Pressure": "Pression", "Recent event families": "Familles d'événements récentes", "Finding severity mix": "Répartition des constats", "Watch state mix": "États des veilles", "Watch states": "États des veilles", "Trigger coverage": "Couverture des déclencheurs",
      "Latest daemon events · grouped by signal family · newest first.": "Derniers événements daemon · groupés par famille · plus récents d'abord.", "Ingress fan-out, pipeline pressure, and recent dimensional mix.": "Diffusion d'entrée, pression du pipeline et dimensions récentes.", "Event count by normalized family in the live ring buffer.": "Nombre d'événements par famille normalisée dans le tampon live.", "Observation count by severity across recent findings.": "Nombre d'observations par sévérité dans les constats récents.", "Current monitor lifecycle states.": "États courants du cycle de vie des moniteurs.", "Active and completed event-driven monitors.": "Moniteurs événementiels actifs et terminés.", "Latest observation results.": "Derniers résultats d'observation.", "Event patterns registered with the monitor event bridge.": "Motifs d'événements enregistrés dans le pont des moniteurs.",
      "Triggers": "Déclencheurs", "Watches": "Veilles", "Pattern": "Motif", "Triggered": "Déclenché", "Last event": "Dernier événement", "Value": "Valeur", "Dimension": "Dimension", "Signal": "Signal", "armed": "armé", "done": "terminé", "suspended": "suspendu", "yes": "oui", "no": "non",
      "signal.family.fs": "fichiers", "signal.family.gateway": "passerelle", "signal.family.ui": "interface", "signal.family.clipboard": "presse-papiers", "signal.family.app": "application", "signal.family.unknown": "inconnu"
    },
    es: {
      "All": "Todo", "connecting…": "conectando", "live": "conectado", "reconnecting…": "reconectando", "seconds ago": "hace {count} s", "minutes ago": "hace {count} min", "hours ago": "hace {count} h",
      "Showing {shown} of {total} recent events.": "Mostrando {shown} de {total} eventos recientes.", "Showing {shown} of {total} {family} events.": "Mostrando {shown} de {total} eventos {family}.",
      "stale build": "build obsoleto", "stale_build_title": "Este servidor LeapBoard (pid {pid}) es anterior al árbol de código actual. Reinícialo para cargar los cambios recientes.",
      "Stream events": "Eventos de flujo", "Active watches": "Vigilancias activas", "Watch portfolio": "Cartera de vigilancias", "Noise suppressed": "Ruido suprimido", "Source dropped": "Fuente descartada", "Reorder pending": "Reordenación pendiente",
      "Signal health summary": "Resumen de salud de señales", "Ingress": "Entrada", "Pressure": "Presión", "Recent event families": "Familias de eventos recientes", "Finding severity mix": "Mezcla de severidad", "Watch state mix": "Estados de vigilancia", "Watch states": "Estados de vigilancia", "Trigger coverage": "Cobertura de disparadores",
      "Latest daemon events · grouped by signal family · newest first.": "Últimos eventos del daemon · agrupados por familia · recientes primero.", "Ingress fan-out, pipeline pressure, and recent dimensional mix.": "Difusión de entrada, presión del pipeline y mezcla dimensional reciente.", "Event count by normalized family in the live ring buffer.": "Conteo de eventos por familia normalizada en el búfer live.", "Observation count by severity across recent findings.": "Conteo de observaciones por severidad en hallazgos recientes.", "Current monitor lifecycle states.": "Estados actuales del ciclo de vida de monitores.", "Active and completed event-driven monitors.": "Monitores por eventos activos y completados.", "Latest observation results.": "Últimos resultados de observación.", "Event patterns registered with the monitor event bridge.": "Patrones de eventos registrados en el puente de monitores.",
      "Triggers": "Disparadores", "Watches": "Vigilancias", "Pattern": "Patrón", "Triggered": "Disparado", "Last event": "Último evento", "Value": "Valor", "Dimension": "Dimensión", "Signal": "Señal", "armed": "armado", "done": "terminado", "suspended": "suspendido", "yes": "sí", "no": "no",
      "signal.family.fs": "archivos", "signal.family.gateway": "gateway", "signal.family.ui": "interfaz", "signal.family.clipboard": "portapapeles", "signal.family.app": "aplicación", "signal.family.unknown": "desconocido"
    },
    ar: {
      "All": "الكل", "connecting…": "جارٍ الاتصال", "live": "متصل", "reconnecting…": "جارٍ إعادة الاتصال", "seconds ago": "قبل {count} ث", "minutes ago": "قبل {count} د", "hours ago": "قبل {count} س",
      "Showing {shown} of {total} recent events.": "عرض {shown} من أصل {total} حدثاً حديثاً.", "Showing {shown} of {total} {family} events.": "عرض {shown} من أصل {total} من أحداث {family}.",
      "stale build": "بناء قديم", "stale_build_title": "خادم LeapBoard (pid {pid}) أقدم من شجرة المصدر الحالية. أعد تشغيله لتحميل التغييرات الأخيرة.",
      "Stream events": "أحداث التدفق", "Active watches": "المراقبات النشطة", "Watch portfolio": "محفظة المراقبات", "Noise suppressed": "الضجيج المحجوب", "Source dropped": "مصدر مُسقط", "Reorder pending": "إعادة الترتيب معلقة",
      "Signal health summary": "ملخص صحة الإشارات", "Ingress": "الدخول", "Pressure": "الضغط", "Recent event families": "عائلات الأحداث الأخيرة", "Finding severity mix": "توزيع شدة النتائج", "Watch state mix": "توزيع حالات المراقبة", "Watch states": "حالات المراقبة", "Trigger coverage": "تغطية المُشغّلات",
      "Latest daemon events · grouped by signal family · newest first.": "أحدث أحداث daemon · مجمعة حسب عائلة الإشارة · الأحدث أولاً.", "Ingress fan-out, pipeline pressure, and recent dimensional mix.": "تفرع الدخول وضغط الأنبوب وتوزيع الأبعاد الأخير.", "Event count by normalized family in the live ring buffer.": "عدد الأحداث حسب العائلة الموحدة في المخزن الحلقي المباشر.", "Observation count by severity across recent findings.": "عدد الملاحظات حسب الشدة في النتائج الأخيرة.", "Current monitor lifecycle states.": "حالات دورة حياة المراقبات الحالية.", "Active and completed event-driven monitors.": "المراقبات الحدثية النشطة والمكتملة.", "Latest observation results.": "أحدث نتائج الرصد.", "Event patterns registered with the monitor event bridge.": "أنماط الأحداث المسجلة في جسر أحداث المراقبة.",
      "Triggers": "المُشغّلات", "Watches": "المراقبات", "Pattern": "النمط", "Triggered": "تم التشغيل", "Last event": "آخر حدث", "Value": "القيمة", "Dimension": "البعد", "Signal": "الإشارة", "armed": "مسلح", "done": "منتهي", "suspended": "معلق", "yes": "نعم", "no": "لا",
      "signal.family.fs": "ملفات", "signal.family.gateway": "بوابة", "signal.family.ui": "واجهة", "signal.family.clipboard": "الحافظة", "signal.family.app": "تطبيق", "signal.family.unknown": "مجهول"
    },
    ru: {
      "All": "Все", "connecting…": "подключение", "live": "подключено", "reconnecting…": "переподключение", "seconds ago": "{count} с назад", "minutes ago": "{count} мин назад", "hours ago": "{count} ч назад",
      "Showing {shown} of {total} recent events.": "Показано {shown} из {total} последних событий.", "Showing {shown} of {total} {family} events.": "Показано {shown} из {total} событий {family}.",
      "stale build": "устаревшая сборка", "stale_build_title": "Сервер LeapBoard (pid {pid}) старее текущего дерева исходников. Перезапустите его, чтобы применить изменения.",
      "Stream events": "События потока", "Active watches": "Активные наблюдения", "Watch portfolio": "Портфель наблюдений", "Noise suppressed": "Шум подавлен", "Source dropped": "Источник отброшен", "Reorder pending": "Ожидает сортировки",
      "Signal health summary": "Сводка здоровья сигналов", "Ingress": "Вход", "Pressure": "Давление", "Recent event families": "Недавние семейства событий", "Finding severity mix": "Важность находок", "Watch state mix": "Состояния наблюдений", "Watch states": "Состояния наблюдений", "Trigger coverage": "Покрытие триггеров",
      "Latest daemon events · grouped by signal family · newest first.": "Последние события daemon · по семействам сигналов · новые первыми.", "Ingress fan-out, pipeline pressure, and recent dimensional mix.": "Входной fan-out, давление конвейера и недавнее распределение измерений.", "Event count by normalized family in the live ring buffer.": "Число событий по нормализованным семействам в live-буфере.", "Observation count by severity across recent findings.": "Число наблюдений по важности среди последних находок.", "Current monitor lifecycle states.": "Текущие состояния жизненного цикла мониторов.", "Active and completed event-driven monitors.": "Активные и завершённые событийные мониторы.", "Latest observation results.": "Последние результаты наблюдений.", "Event patterns registered with the monitor event bridge.": "Шаблоны событий, зарегистрированные в мосте мониторов.",
      "Triggers": "Триггеры", "Watches": "Наблюдения", "Pattern": "Шаблон", "Triggered": "Сработал", "Last event": "Последнее событие", "Value": "Значение", "Dimension": "Измерение", "Signal": "Сигнал", "armed": "взведено", "done": "готово", "suspended": "приостановлено", "yes": "да", "no": "нет",
      "signal.family.fs": "файлы", "signal.family.gateway": "шлюз", "signal.family.ui": "интерфейс", "signal.family.clipboard": "буфер", "signal.family.app": "приложение", "signal.family.unknown": "неизвестно"
    }
  };
  Object.entries(I18N_PATCH).forEach(([lang, patch]) => {
    I18N[lang] = Object.assign({}, I18N.en || {}, I18N[lang] || {}, patch);
  });

  function t(key) { return (I18N[locale] && I18N[locale][key]) || (I18N.en && I18N.en[key]) || key; }
  function tx(value) { return typeof value === "string" ? t(value) : value; }
  function fmt(key, vars) {
    return t(key).replace(/\{(\w+)\}/g, (_m, name) => (vars && vars[name] != null ? String(vars[name]) : ""));
  }

  function applyLocale() {
    document.documentElement.lang = locale === "zh" ? "zh-CN" : locale;
    document.documentElement.dir = locale === "ar" ? "rtl" : "ltr";
    if (localeEl) localeEl.value = locale;
    document.querySelectorAll("[data-i18n]").forEach((node) => { node.textContent = t(node.dataset.i18n); });
  }

  function setConnectionStatus(key) {
    if (!statusEl) return;
    statusEl.dataset.i18n = key;
    statusEl.textContent = t(key);
  }

  function api(path) {
    const url = new URL(path, location.origin);
    url.searchParams.set("token", TOKEN);
    return url.toString();
  }

  async function fetchView(intent) {
    var prevTemplate = current.template;
    current = Object.assign({}, current, intent || {});
    const url = new URL("/api/view", location.origin);
    url.searchParams.set("token", TOKEN);
    Object.entries(current).forEach(([k, v]) => v && url.searchParams.set(k, v));
    try {
      const resp = await fetch(url.toString());
      if (!resp.ok) throw new Error("HTTP " + resp.status);
      const spec = await resp.json();
      render(spec);
      renderNav(spec.meta || {});
      renderServerHealth(spec.meta || {});
      // Manage signal auto-refresh lifecycle on template switch
      var newTemplate = (spec.meta && spec.meta.active_template) || current.template || "";
      if (newTemplate === "signals") {
        startSignalAutoRefresh();
        injectSignalRefreshBtn();
      } else if (prevTemplate === "signals" && newTemplate !== "signals") {
        stopSignalAutoRefresh();
      }
    } catch (err) {
      rootEl.innerHTML = '<div class="empty">' + esc(t("Failed to load view") + ": " + String(err)) + "</div>";
    }
  }

  // Long-lived-process staleness: warns when this dashboard server process
  // predates the current source tree (see leapflow.utils.build_info). Purely
  // informational — the page still renders whatever data the stale process
  // returns; this just tells the developer *why* it might look wrong.
  function renderServerHealth(meta) {
    if (!statusEl) return;
    var old = document.getElementById("server-stale-badge");
    if (old) old.remove();
    var server = meta.server;
    if (!server || server.stale !== true) return;
    var build = server.build || {};
    var badge = el("span", "server-stale-badge");
    badge.id = "server-stale-badge";
    badge.textContent = "\u26a0 " + t("stale build");
    badge.title = fmt("stale_build_title", { pid: build.pid || "?" });
    statusEl.insertAdjacentElement("afterend", badge);
  }

  // Template switcher: the current session, rendered through each lens.
  function renderNav(meta) {
    const nav = document.getElementById("nav");
    if (!nav) return;
    const hidden = new Set(Array.isArray(meta.hidden_templates) ? meta.hidden_templates : []);
    HIDDEN_NAV_TEMPLATES.forEach((name) => hidden.add(name));
    const seen = new Set();
    const names = (Array.isArray(meta.templates) ? meta.templates : []).filter((name) => {
      name = String(name || "");
      if (!name || hidden.has(name) || seen.has(name)) return false;
      seen.add(name);
      return true;
    });
    const active = meta.active_template || "";
    nav.innerHTML = "";
    names.forEach((name) => {
      const a = el("a", name === active ? "active" : "");
      a.href = "#";
      a.textContent = name;
      a.addEventListener("click", (ev) => { ev.preventDefault(); fetchView({ template: name }); });
      nav.appendChild(a);
    });
  }

  async function postAction(action) {
    if (action && action.kind === "nav") { handleNav(action); return; }
    try {
      const resp = await fetch(api("/api/action"), {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Dashboard-Token": TOKEN },
        body: JSON.stringify(action),
      });
      const result = await resp.json();
      if (action.kind === "rpc") fetchView(); // reflect control changes
      return result;
    } catch (err) {
      toast({ title: t("Action failed"), summary: String(err), severity: "alert" });
    }
  }

  // nav actions are purely client-side (no server round-trip).
  function handleNav(action) {
    const p = action.params || {};
    if (action.name === "openLink" && p.url) window.open(p.url, "_blank", "noopener");
  }

  function esc(s) {
    return String(s == null ? "" : s).replace(/[&<>"]/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  // ── Renderers keyed by catalog type; unknown types fall back to text ──
  function el(tag, cls, html) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (html != null) e.innerHTML = html;
    return e;
  }

  function renderChildren(node, parent) {
    (node.children || []).forEach((c) => parent.appendChild(renderNode(c)));
    return parent;
  }

  function bindAction(dom, node) {
    if (node.action) {
      dom.style.cursor = "pointer";
      dom.addEventListener("click", (ev) => { ev.stopPropagation(); postAction(node.action); });
    }
    return dom;
  }

  // Escape-hatch renderers for the `Custom` component, keyed by props.render.
  const CUSTOM_RENDERERS = {
    candlestick: (p) => { const data = Array.isArray(p.data) ? p.data : [];
      const d = el("div", "mini-chart card"); d.appendChild(el("div", "card-title", t("Candlestick")));
      d.appendChild(el("div", "chart-placeholder", esc(data.length + " " + t("Series")))); return d; },
    gauge: (p) => renderGaugeValue(p.label || "Gauge", p.data),
    signalTimeline: renderSignalTimeline,
  };

  function asArray(value) { return Array.isArray(value) ? value : []; }

  function severityOf(item) { return String((item && item.severity) || "info").toLowerCase(); }

  function severityCounts(items) {
    return asArray(items).reduce((acc, item) => { const sev = severityOf(item); acc[sev] = (acc[sev] || 0) + 1; return acc; }, {});
  }

  // Academic numbering: build a caption node ("Fig. N" / "Table N") + text.
  function captionInto(host, label, text) {
    const num = el("span", "fignum"); num.textContent = label; host.appendChild(num);
    if (text) host.appendChild(document.createTextNode(String(text)));
    return host;
  }
  function figcaption(text) { return captionInto(el("figcaption", "figcaption"), "Fig. " + (++figSeq), tx(text)); }
  function tableCaption(text) { return captionInto(document.createElement("caption"), "Table " + (++tblSeq), tx(text)); }
  function chartNode(dom, props) { if (props && props.caption) dom.appendChild(figcaption(props.caption)); return dom; }

  // Layout helpers: template-driven grid column count and child spans, so a view
  // can compose dense asymmetric grids without introducing new component types.
  function _clampInt(value, lo, hi) { const n = parseInt(value, 10); return Number.isFinite(n) ? Math.max(lo, Math.min(hi, n)) : 0; }
  function gridCols(props) { const c = _clampInt(props.cols, 2, 6); return c ? " cols-" + c : ""; }
  function applySpan(dom, props) { const s = _clampInt(props.span, 2, 4); if (s && dom && dom.classList) dom.classList.add("span-" + s); }

  function signalFamily(item) {
    var raw = String((item && (item.family || item.event_type || item.title)) || "unknown").replace(":", ".");
    return raw.split(".", 1)[0] || "unknown";
  }

  function signalTimestamp(item) {
    var raw = Number(item && item.ts);
    if (!Number.isFinite(raw) || raw <= 0) return 0;
    return raw < 100000000000 ? raw * 1000 : raw;
  }

  function signalFamilyLabel(family) {
    const key = "signal.family." + String(family || "unknown");
    const label = t(key);
    return label === key ? String(family || "unknown") : label;
  }

  function signalTimeLabel(item) {
    var ms = signalTimestamp(item);
    if (!ms) return "--:--:--";
    var d = new Date(ms);
    var clock = d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
    var age = Math.max(0, Date.now() - ms);
    if (age < 60000) return clock + " · " + fmt("seconds ago", { count: Math.max(0, Math.round(age / 1000)) });
    if (age < 3600000) return clock + " · " + fmt("minutes ago", { count: Math.round(age / 60000) });
    return clock + " · " + fmt("hours ago", { count: Math.round(age / 3600000) });
  }

  function normalizeSignalItems(data) {
    return asArray(data).filter((it) => it && typeof it === "object")
      .map((it) => Object.assign({}, it, { family: signalFamily(it), _ts: signalTimestamp(it) }))
      .sort((a, b) => (b._ts || 0) - (a._ts || 0));
  }

  function signalCategories(items) {
    const counts = {}; items.forEach((it) => { counts[it.family] = (counts[it.family] || 0) + 1; });
    return [{ key: "all", label: t("All"), count: items.length }]
      .concat(Object.keys(counts).sort().map((key) => ({ key, label: signalFamilyLabel(key), count: counts[key] })));
  }

  function renderSignalTimeline(props) {
    const box = el("div", "signal-timeline");
    box._signalTimelineOptions = { maxItems: _clampInt(props.max_items || props.maxItems || 12, 1, 24) || 12 };
    renderSignalTimelineInto(box, props.data || []);
    window._signalStream = normalizeSignalItems(props.data || []);
    return box;
  }

  function renderSignalTimelineInto(box, data) {
    const opts = box._signalTimelineOptions || { maxItems: 12 };
    const items = normalizeSignalItems(data);
    const categories = signalCategories(items);
    var active = window._signalTimelineActiveFamily || box.dataset.activeFamily || "all";
    if (!categories.some((c) => c.key === active)) active = "all";
    window._signalTimelineActiveFamily = active;
    box.dataset.activeFamily = active;
    box.innerHTML = "";
    if (!items.length) { box.appendChild(el("div", "empty-inline", esc(t("No entries.")))); return box; }

    const tabs = el("div", "signal-tabs");
    categories.forEach((cat) => {
      const btn = el("button", "signal-tab" + (cat.key === active ? " active" : ""));
      btn.type = "button"; btn.textContent = cat.label + " " + cat.count;
      btn.addEventListener("click", () => { window._signalTimelineActiveFamily = cat.key; renderSignalTimelineInto(box, items); });
      tabs.appendChild(btn);
    });
    box.appendChild(tabs);

    const filtered = active === "all" ? items : items.filter((it) => it.family === active);
    const shown = filtered.slice(0, opts.maxItems);
    const list = el("div", "signal-stream-list timeline");
    shown.forEach((it) => {
      const row = el("div", "signal-row timeline-item sev-" + severityOf(it));
      const meta = el("div", "signal-event-meta");
      meta.appendChild(el("span", "signal-time", esc(signalTimeLabel(it))));
      meta.appendChild(el("span", "signal-family", esc(signalFamilyLabel(it.family))));
      row.appendChild(meta);
      row.appendChild(el("div", "timeline-title signal-type", esc(it.event_type || it.title || "")));
      if (it.source || it.summary) row.appendChild(el("div", "summary signal-source", esc(it.source || it.summary)));
      list.appendChild(row);
    });
    box.appendChild(list);
    const footer = el("div", "signal-stream-footer");
    footer.textContent = active === "all"
      ? fmt("Showing {shown} of {total} recent events.", { shown: shown.length, total: filtered.length })
      : fmt("Showing {shown} of {total} {family} events.", { shown: shown.length, total: filtered.length, family: signalFamilyLabel(active) });
    box.appendChild(footer);
    return box;
  }

  // Format the storyline like a paper abstract: bold lead-in sentence + body.
  function renderAbstract(text) {
    const s = String(text == null ? "" : text).trim();
    const box = el("div", "abstract");
    if (!s) return box;
    const idx = s.search(/[.!?\u3002\uff01\uff1f]/);
    if (idx > -1 && idx < 160) {
      const lead = el("span", "lead"); lead.textContent = s.slice(0, idx + 1); box.appendChild(lead);
      const rest = s.slice(idx + 1).trim();
      if (rest) box.appendChild(document.createTextNode(" " + rest));
    } else {
      box.textContent = s;
    }
    return box;
  }

  // List: a definition list when items carry a summary, else compact bullets.
  function renderList(node) {
    const items = asArray((node.props || {}).data);
    if (!items.length) return el("div", "empty-inline", esc(t("No entries.")));
    const structured = items.some((it) => it && typeof it === "object" && (it.summary || it.detail || it.value));
    if (structured) {
      const dl = el("dl", "dl");
      items.forEach((it) => {
        const obj = it && typeof it === "object";
        dl.appendChild(el("dt", null, esc(obj ? (it.title || it.name || it.label || "") : it)));
        dl.appendChild(el("dd", null, esc(obj ? (it.summary || it.detail || it.value || "") : "")));
      });
      return dl;
    }
    const ul = el("ul", "insight-list");
    items.forEach((it) => ul.appendChild(el("li", null, esc(typeof it === "object" ? (it.title || it.summary || JSON.stringify(it)) : tx(it)))));
    return ul;
  }

  function renderGaugeValue(label, value) {
    const d = el("div", "stat gauge-stat");
    d.appendChild(el("div", "label", esc(tx(label || "Gauge"))));
    d.appendChild(el("div", "value", esc(value != null && value !== "" ? value : "\u2014")));
    return d;
  }

  function svgEl(tag) { return document.createElementNS("http://www.w3.org/2000/svg", tag); }

  // Distribution bars (label -> value) or, as a fallback, the severity mix of a
  // findings/insights array. Real values only — never synthetic.
  function renderChartBars(data, title) {
    const arr = asArray(data);
    let dist = null;
    if (arr.length && arr[0] && Array.isArray(arr[0].items)) dist = asArray(arr[0].items);
    else if (arr.length && arr.every((it) => it && typeof it === "object" && "value" in it && ("label" in it || "name" in it))) dist = arr;
    const d = el("div", "chart card");
    if (title) d.appendChild(el("div", "card-title", esc(tx(title))));
    let rows; let severity = false;
    if (dist) {
      rows = dist.map((it) => ({ key: "", label: String(it.label || it.name || ""), value: Number(it.value) || 0 }));
    } else {
      severity = true;
      const counts = severityCounts(data);
      rows = ["alert", "notable", "info"].map((key) => ({ key, label: t(key), value: counts[key] || 0 }));
    }
    const max = Math.max(1, ...rows.map((r) => r.value));
    rows.forEach((row) => {
      const line = el("div", "bar-row");
      line.appendChild(el("span", "bar-label", esc(tx(row.label))));
      const track = el("span", "bar-track");
      const fill = el("span", "bar-fill" + (severity ? " sev-" + row.key : "")); fill.style.width = Math.round((row.value / max) * 100) + "%";
      track.appendChild(fill); line.appendChild(track); line.appendChild(el("span", "bar-value", esc(row.value))); d.appendChild(line);
    });
    return d;
  }

  // Normalize a bound value into series groups [{label, points:[{x,y}]}].
  function seriesGroups(data) {
    const arr = asArray(data);
    if (arr.length && arr[0] && Array.isArray(arr[0].points)) return arr;
    const pts = arr.filter((p) => p && typeof p === "object" && "y" in p);
    return pts.length ? [{ label: "", points: pts }] : [];
  }

  // Real line/area chart: plots actual {x,y} points, auto-scaled. No fake data.
  function renderSparkline(data, title, opts) {
    const d = el("div", "chart card");
    if (title) d.appendChild(el("div", "card-title", esc(tx(title))));
    const groups = seriesGroups(data).slice(0, 4)
      .map((g) => ({ label: String(g.label || ""), points: asArray(g.points).map((p, i) => ({ x: p.x != null ? p.x : i, y: Number(p.y) })).filter((p) => Number.isFinite(p.y)) }))
      .filter((g) => g.points.length >= 2);
    if (!groups.length) { d.appendChild(el("div", "chart-placeholder", esc(t("No entries.")))); return d; }
    const ys = []; groups.forEach((g) => g.points.forEach((p) => ys.push(p.y)));
    const min = Math.min.apply(null, ys), max = Math.max.apply(null, ys), span = (max - min) || 1;
    const W = 320, H = 96, pad = 4;
    const svg = svgEl("svg"); svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("class", "sparkline");
    const strokes = ["var(--accent)", "var(--info)", "var(--notable)", "var(--faint)"];
    groups.forEach((g, gi) => {
      const n = Math.max(1, g.points.length - 1);
      const coords = g.points.map((p, i) => (i * (W / n)).toFixed(1) + "," + (H - pad - ((p.y - min) / span) * (H - pad * 2)).toFixed(1)).join(" ");
      if (opts && opts.area) {
        const poly = svgEl("polygon"); poly.setAttribute("points", "0," + (H - pad) + " " + coords + " " + W + "," + (H - pad));
        poly.setAttribute("style", "fill:" + strokes[gi % strokes.length] + ";opacity:.12;stroke:none"); svg.appendChild(poly);
      }
      const line = svgEl("polyline"); line.setAttribute("points", coords);
      line.setAttribute("style", "stroke:" + strokes[gi % strokes.length]); svg.appendChild(line);
    });
    d.appendChild(svg);
    // Always name the line(s) so the chart is self-describing, even for a single
    // series; skip blank labels.
    const labeled = groups.filter((g) => g.label);
    if (labeled.length) { const lg = el("div", "legend"); labeled.forEach((g) => lg.appendChild(el("span", "legend-item", esc(g.label)))); d.appendChild(lg); }
    return d;
  }

  // Real candlestick: OHLC bars from captured market data, auto-scaled.
  function renderCandlestick(data, title) {
    const arr = asArray(data);
    let bars = (arr.length && arr[0] && Array.isArray(arr[0].bars)) ? asArray(arr[0].bars) : arr;
    bars = bars.map((b) => ({ o: Number(b && b.o), h: Number(b && b.h), l: Number(b && b.l), c: Number(b && b.c) }))
      .filter((b) => Number.isFinite(b.o) && Number.isFinite(b.h) && Number.isFinite(b.l) && Number.isFinite(b.c));
    const d = el("div", "chart card");
    if (title) d.appendChild(el("div", "card-title", esc(tx(title))));
    if (bars.length < 2) { d.appendChild(el("div", "chart-placeholder", esc(t("No entries.")))); return d; }
    const lo = Math.min.apply(null, bars.map((b) => b.l)), hi = Math.max.apply(null, bars.map((b) => b.h)), span = (hi - lo) || 1;
    const W = 320, H = 120, pad = 6, step = W / bars.length, bw = Math.max(2, step * 0.6);
    const y = (v) => H - pad - ((v - lo) / span) * (H - pad * 2);
    const svg = svgEl("svg"); svg.setAttribute("viewBox", "0 0 " + W + " " + H); svg.setAttribute("preserveAspectRatio", "none"); svg.setAttribute("class", "sparkline");
    bars.forEach((b, i) => {
      const cx = i * step + step / 2, color = b.c >= b.o ? "var(--info)" : "var(--alert)";
      const wick = svgEl("line"); wick.setAttribute("x1", cx.toFixed(1)); wick.setAttribute("x2", cx.toFixed(1));
      wick.setAttribute("y1", y(b.h).toFixed(1)); wick.setAttribute("y2", y(b.l).toFixed(1));
      wick.setAttribute("style", "stroke:" + color + ";stroke-width:1"); svg.appendChild(wick);
      const top = y(Math.max(b.o, b.c)), bot = y(Math.min(b.o, b.c));
      const rect = svgEl("rect"); rect.setAttribute("x", (cx - bw / 2).toFixed(1)); rect.setAttribute("y", top.toFixed(1));
      rect.setAttribute("width", bw.toFixed(1)); rect.setAttribute("height", Math.max(1, bot - top).toFixed(1));
      rect.setAttribute("style", "fill:" + color); svg.appendChild(rect);
    });
    d.appendChild(svg); return d;
  }

  function renderPie(data, title) {
    const counts = severityCounts(data); const total = Math.max(1, (counts.alert || 0) + (counts.notable || 0) + (counts.info || 0));
    const d = el("div", "chart card pie-card");
    if (title) d.appendChild(el("div", "card-title", esc(tx(title))));
    const pie = el("div", "pie");
    pie.style.background = "conic-gradient(var(--alert) 0 " + ((counts.alert || 0) / total * 100) + "%, var(--notable) 0 " + (((counts.alert || 0) + (counts.notable || 0)) / total * 100) + "%, var(--info) 0 100%)";
    d.appendChild(pie); d.appendChild(renderLegend(["alert", "notable", "info"], counts)); return d;
  }

  function renderLegend(keys, counts) {
    const box = el("div", "legend");
    keys.forEach((key) => box.appendChild(el("span", "legend-item sev-" + key, esc(t(key) + " " + (counts[key] || 0)))));
    return box;
  }

  function renderTable(node) {
    const p = node.props || {}; const rows = asArray(p.data); const cols = asArray(p.columns);
    if (!rows.length) return el("div", "empty-inline", esc(t("No entries.")));
    const table = el("table", "data-table");
    if (p.caption) table.appendChild(tableCaption(p.caption));
    const head = document.createElement("thead"); const headRow = document.createElement("tr");
    cols.forEach((c) => headRow.appendChild(el("th", null, esc(tx(c.label || c.key || c))))); head.appendChild(headRow); table.appendChild(head);
    const body = document.createElement("tbody"); rows.forEach((row) => { const tr = document.createElement("tr"); cols.forEach((c) => { const v = row && row[c.key || c] != null ? row[c.key || c] : ""; tr.appendChild(el("td", null, esc(tx(v)))); }); body.appendChild(tr); }); table.appendChild(body); return table;
  }

  function renderTimeline(node) {
    const items = asArray((node.props || {}).data); const d = el("div", "timeline");
    items.forEach((it) => { const row = el("div", "timeline-item sev-" + severityOf(it)); row.appendChild(el("div", "timeline-title", esc(it.title || ""))); if (it.summary) row.appendChild(el("div", "summary", esc(it.summary))); d.appendChild(row); });
    return d;
  }

  const RENDERERS = {
    Page: (n) => { const d = el("div", "page");
      const t0 = (n.props && n.props.title); if (t0) d.appendChild(el("div", "page-title", esc(tx(t0))));
      return renderChildren(n, d); },
    Section: (n) => { const p = n.props || {}; const d = el("section", "section");
      if (p.title) d.appendChild(el("div", "section-title", esc(tx(p.title))));
      if (p.subtitle) d.appendChild(el("div", "section-subtitle", esc(tx(p.subtitle))));
      return renderChildren(n, d); },
    Grid: (n) => renderChildren(n, el("div", "grid" + gridCols(n.props || {}))),
    Row: (n) => { const v = (n.props || {}).variant; const cls = v === "metrics" ? " metric-strip" : (v === "meta" ? " row-meta" : ""); return renderChildren(n, el("div", "row" + cls)); },
    Col: (n) => renderChildren(n, el("div", "col")),
    Card: (n) => { const d = el("div", "card");
      const title = n.props && n.props.title; if (title) d.appendChild(el("div", "card-title", esc(tx(title))));
      const kicker = n.props && n.props.kicker; if (kicker) d.appendChild(el("div", "kicker", esc(tx(kicker))));
      return renderChildren(n, d); },
    Board: (n) => { const d = el("div", "board");
      const title = n.props && n.props.title; if (title) d.appendChild(el("div", "board-title", esc(tx(title))));
      return renderChildren(n, d); },
    Toolbar: (n) => renderChildren(n, el("div", "toolbar")),
    Stat: (n) => { const p = n.props || {}; const d = el("div", "stat");
      d.appendChild(el("div", "label", esc(tx(p.label))));
      const v = (p.value != null && p.value !== "") ? (p.i18nValue ? tx(p.value) : p.value) : "\u2014";
      d.appendChild(el("div", "value", esc(v)));
      return d; },
    Markdown: (n) => el("div", "md prose", esc((n.props || {}).text)),
    StoryPanel: (n) => { const p = n.props || {}; const d = el("div", "card story-panel");
      d.appendChild(el("div", "card-title", esc(tx(p.title || "Storyline"))));
      d.appendChild(renderAbstract(p.text)); return d; },
    List: renderList,
    SuggestionChips: (n) => { const items = ((n.props || {}).data) || []; const d = el("div", "chips");
      asArray(items).forEach((it) => d.appendChild(el("button", null, esc(it)))); return d; },
    Gauge: (n) => { const p = n.props || {}; return renderGaugeValue(p.label || "Gauge", p.data != null ? p.data : p.value); },
    ProgressBar: (n) => { const p = n.props || {}; const d = el("div", "progress"); const fill = el("span", "progress-fill"); fill.style.width = Math.max(0, Math.min(100, Number(p.value || 0))) + "%"; d.appendChild(fill); return d; },
    Badge: (n) => { const p = n.props || {}; return el("span", "badge sev-" + String(p.tone || p.severity || "info").toLowerCase(), esc(tx(p.label || p.text || "info"))); },
    Table: renderTable,
    Timeline: renderTimeline,
    BarChart: (n) => chartNode(renderChartBars((n.props || {}).data, (n.props || {}).title || "Severity mix"), n.props || {}),
    AreaChart: (n) => chartNode(renderSparkline((n.props || {}).data, (n.props || {}).title, { area: true }), n.props || {}),
    LineChart: (n) => chartNode(renderSparkline((n.props || {}).data, (n.props || {}).title), n.props || {}),
    Sparkline: (n) => chartNode(renderSparkline((n.props || {}).data, (n.props || {}).title), n.props || {}),
    CandlestickChart: (n) => chartNode(renderCandlestick((n.props || {}).data, (n.props || {}).title || "Candlestick"), n.props || {}),
    PieChart: (n) => chartNode(renderPie((n.props || {}).data, (n.props || {}).title || "Severity mix"), n.props || {}),
    Quote: (n) => { const p = n.props || {}; const q = el("blockquote", "quote", esc(p.text)); if (p.source) q.appendChild(el("cite", null, esc(p.source))); return q; },
    CitationList: (n) => { const items = asArray((n.props || {}).data); const ol = el("ol", "citations"); items.forEach((it) => ol.appendChild(el("li", null, esc(it.label || it.title || it.url || it)))); return ol; },
    EntityGraph: (n) => { const items = asArray((n.props || {}).data); const d = el("div", "entity-cloud"); items.forEach((it) => d.appendChild(el("span", "badge", esc(it.name || it.title || it)))); return d; },
    Custom: (n) => { const p = n.props || {}; const fn = CUSTOM_RENDERERS[p.render];
      return fn ? fn(p) : el("div", "card md", esc(t("Custom") + ": " + (p.render || "?"))); },
    FindingCard: renderFinding,
    InsightCard: renderFinding,
    Button: (n) => el("button", null, esc(tx((n.props || {}).label || (n.props || {}).text || "Action"))),
    FilterBar: (n) => el("div", "toolbar", ""),
  };

  function renderFinding(node) {
    const p = node.props || {};
    const sev = (p.severity || "info").toLowerCase();
    const d = el("div", "finding sev-" + sev);
    d.appendChild(el("div", "sev", esc(t(sev))));
    d.appendChild(el("div", "card-title", esc(p.title)));
    if (p.summary) d.appendChild(el("div", "summary", esc(p.summary)));
    return d;
  }

  function renderNode(node) {
    if (!node || typeof node !== "object") return el("div", "md", esc(node));
    const fn = RENDERERS[node.type];
    let dom;
    if (fn) {
      dom = fn(node);
    } else {
      dom = el("div", "card"); // safe fallback for unknown catalog types
      dom.appendChild(el("div", "sev", esc(node.type || "unknown")));
      dom.appendChild(el("div", "md", esc((node.props && node.props.text) || JSON.stringify(node.props || {}))));
      renderChildren(node, dom);
    }
    applySpan(dom, node.props || {});
    return bindAction(dom, node);
  }

  function render(spec) {
    rootEl.innerHTML = "";
    figSeq = 0; tblSeq = 0;
    (spec.root || []).forEach((n) => rootEl.appendChild(renderNode(n)));
    if (!(spec.root || []).length) rootEl.appendChild(el("div", "empty", esc(t("No content yet."))));
    document.title = spec.title ? spec.title + " \u00b7 LeapBoard" : "LeapBoard";
  }

  function toast(finding) {
    const sev = (finding.severity || "info").toLowerCase();
    const t = el("div", "toast sev-" + sev);
    t.appendChild(el("div", "card-title", esc(finding.title)));
    if (finding.summary) t.appendChild(el("div", "summary", esc(finding.summary)));
    toastsEl.appendChild(t);
    setTimeout(() => t.remove(), 8000);
  }

  // ── Live updates over WebSocket ──
  function connectWS() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(proto + "://" + location.host + "/ws?token=" + encodeURIComponent(TOKEN));
    ws.onopen = () => { setConnectionStatus("live"); };
    ws.onclose = () => { setConnectionStatus("reconnecting…"); setTimeout(connectWS, 3000); };
    ws.onmessage = (ev) => {
      let msg; try { msg = JSON.parse(ev.data); } catch (_) { return; }
      if (msg.type === "monitor.finding") { toast(msg.payload || {}); fetchView(); }
      else if (msg.type === "watch.state") { fetchView(); }
      else if (msg.type === "signal.stream") {
        // Append to local signal stream buffer (max 50)
        if (!window._signalStream) window._signalStream = [];
        var payload = msg.payload || {};
        window._signalStream.push(payload);
        if (window._signalStream.length > 50) window._signalStream.shift();
        updateSignalTimeline(window._signalStream);
        // Increment live event counter
        incrementSignalCounter();
      }
      else if (msg.type === "view.replace" && msg.spec) { render(msg.spec); }
    };
  }

  function updateSignalTimeline(stream) {
    if (current.template !== "signals") return;
    var custom = document.querySelector(".signal-timeline");
    if (custom) { renderSignalTimelineInto(custom, stream); return; }
    var container = document.querySelector(".timeline");
    if (!container) return;
    container.innerHTML = "";
    normalizeSignalItems(stream).slice(0, 12).forEach(function (item) {
      var row = el("div", "timeline-item sev-info");
      row.appendChild(el("div", "timeline-title", esc(item.event_type || item.title || "")));
      if (item.source || item.summary) row.appendChild(el("div", "summary", esc(item.source || item.summary)));
      container.appendChild(row);
    });
  }

  if (localeEl) {
    localeEl.addEventListener("change", () => {
      locale = localeEl.value || "en";
      localStorage.setItem("leapboard.locale", locale);
      applyLocale();
      fetchView();
    });
  }

  applyLocale();
  fetchView();
  connectWS();
})();

/**
 * dailyNewsDigest2 フロントエンド制御スクリプト (app.js)
 *
 * 目的（Why）:
 *   初回起動確認（はい/いいえ/キャンセル）、手動ニュース更新、保存済み記事からの再選別、
 *   既読/未読切替、および設定更新を非同期API通信で制御する。
 * 注意（Caution）:
 *   APIキーは一切ブラウザ側で保持・送信しない（指示書 §11, §21）。
 * 変更履歴（Change Log）:
 *   2026-11-02 - 初版作成（Step 2-3）
 */

(function () {
  "use strict";

  function showStatus(message, isError) {
    const banner = document.getElementById("status-banner");
    if (!banner) return;
    banner.textContent = message;
    banner.classList.remove("hidden");
    banner.style.backgroundColor = isError ? "#fee2e2" : "#eff6ff";
    banner.style.color = isError ? "#991b1b" : "#1e3a8a";
  }

  function getCheckedValues(checkboxName) {
    const boxes = document.querySelectorAll(`input[name="${checkboxName}"]:checked`);
    return Array.from(boxes).map((el) => el.value);
  }

  function checkAllBoxes(checkboxName) {
    const boxes = document.querySelectorAll(`input[name="${checkboxName}"]`);
    boxes.forEach((el) => {
      el.checked = true;
    });
  }

  // --- 初回起動確認画面（onboarding.html） ---
  const btnSelectAllOnboarding = document.getElementById("btn-onboarding-select-all");
  if (btnSelectAllOnboarding) {
    btnSelectAllOnboarding.addEventListener("click", function () {
      checkAllBoxes("selected_categories");
    });
  }

  const btnPreview = document.getElementById("btn-run-onboarding-preview");
  if (btnPreview) {
    btnPreview.addEventListener("click", async function () {
      const dateEl = document.getElementById("onboarding-target-date");
      const cats = getCheckedValues("selected_categories");
      if (!dateEl || !dateEl.value) {
        showStatus("取得対象日を入力してください。", true);
        return;
      }
      if (cats.length === 0) {
        showStatus("ニュースカテゴリを1つ以上選択してください。", true);
        return;
      }

      showStatus("RSSから対象日の記事を一時集計しています...");
      try {
        const res = await fetch("/api/onboarding/preview", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_date: dateEl.value,
            selected_categories: cats,
            select_all: false,
          }),
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || "プレビュー集計に失敗しました");
        }
        const data = await res.json();
        document.getElementById("onboarding-preview-token").value = data.preview_token;
        document.getElementById("stat-total-fetched").textContent = data.total_fetched_count;
        document.getElementById("stat-deduped-count").textContent = data.deduped_count;

        const warnBox = document.getElementById("onboarding-range-warning");
        if (data.out_of_range_notice) {
          warnBox.textContent = data.out_of_range_notice;
          warnBox.classList.remove("hidden");
        } else {
          warnBox.classList.add("hidden");
        }

        const countList = document.getElementById("onboarding-category-counts");
        countList.innerHTML = "";
        Object.entries(data.category_tentative_counts || {}).forEach(([catKey, count]) => {
          const li = document.createElement("li");
          li.textContent = `${catKey}: ${count} 件`;
          countList.appendChild(li);
        });

        document.getElementById("onboarding-confirm-box").classList.remove("hidden");
        showStatus("集計が完了しました。注意事項を確認のうえ「はい / いいえ / キャンセル」を選択してください。");
      } catch (e) {
        showStatus(e.message, true);
      }
    });
  }

  document.querySelectorAll("[data-decision]").forEach((btn) => {
    btn.addEventListener("click", async function () {
      const decision = btn.getAttribute("data-decision");
      const token = document.getElementById("onboarding-preview-token")?.value || "";
      const cats = getCheckedValues("selected_categories");

      try {
        const res = await fetch("/api/onboarding/decision", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            preview_token: token,
            decision: decision,
            selected_categories: cats,
          }),
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || "処理に失敗しました");
        }
        const data = await res.json();

        if (decision === "yes" && data.redirect_to) {
          window.location.href = data.redirect_to;
        } else if (decision === "cancel" && data.redirect_to) {
          window.location.href = data.redirect_to;
        } else if (decision === "no") {
          const previewBox = document.getElementById("onboarding-title-preview-box");
          const titleList = document.getElementById("onboarding-title-list");
          titleList.innerHTML = "";
          (data.preview_titles || []).forEach((item) => {
            const li = document.createElement("li");
            li.textContent = `[${item.source_name}] ${item.title} (${item.published_at_jst_str})`;
            titleList.appendChild(li);
          });
          previewBox.classList.remove("hidden");
          showStatus("「いいえ」が選択されました。DB保存およびAI処理を行わずタイトルのみ表示しています。");
        }
      } catch (e) {
        showStatus(e.message, true);
      }
    });
  });

  // --- 基本画面（index.html） ---
  const btnManualRefresh = document.getElementById("btn-manual-refresh");
  if (btnManualRefresh) {
    btnManualRefresh.addEventListener("click", async function () {
      showStatus("RSSから最新記事を取得しています...");
      try {
        const cats = getCheckedValues("main_selected_categories");
        const res = await fetch("/api/news/refresh", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            selected_categories: cats.length ? cats : null,
          }),
        });
        if (!res.ok) throw new Error("手動更新に失敗しました");
        window.location.reload();
      } catch (e) {
        showStatus(e.message, true);
      }
    });
  }

  const btnSelectAllMain = document.getElementById("btn-select-all-categories");
  if (btnSelectAllMain) {
    btnSelectAllMain.addEventListener("click", function () {
      checkAllBoxes("main_selected_categories");
    });
  }

  const btnRecategorize = document.getElementById("btn-apply-recategorize");
  if (btnRecategorize) {
    btnRecategorize.addEventListener("click", async function () {
      const cats = getCheckedValues("main_selected_categories");
      const displayCount = parseInt(document.getElementById("select-display-count")?.value || "15", 10);
      const sortOrder = document.getElementById("select-sort-order")?.value || "importance";
      if (cats.length === 0) {
        showStatus("カテゴリを1つ以上選択してください。", true);
        return;
      }
      showStatus("保存済み記事を再利用して再選別しています（RSS再取得なし）...");
      try {
        const res = await fetch("/api/news/recategorize", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            selected_categories: cats,
            display_count: displayCount,
            sort_order: sortOrder,
          }),
        });
        if (!res.ok) throw new Error("再選別に失敗しました");
        window.location.reload();
      } catch (e) {
        showStatus(e.message, true);
      }
    });
  }

  document.querySelectorAll(".btn-toggle-read").forEach((btn) => {
    btn.addEventListener("click", async function () {
      const articleId = btn.getAttribute("data-article-id");
      const currentRead = btn.getAttribute("data-is-read") === "true";
      const nextRead = !currentRead;
      try {
        const res = await fetch(`/api/articles/${articleId}/read`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_read: nextRead }),
        });
        if (!res.ok) throw new Error("既読状態の更新に失敗しました");
        btn.setAttribute("data-is-read", nextRead ? "true" : "false");
        btn.textContent = nextRead ? "既読（未読に戻す）" : "未読（既読にする）";
        const card = btn.closest(".news-card");
        if (card) {
          card.classList.toggle("is-read", nextRead);
        }
      } catch (e) {
        showStatus(e.message, true);
      }
    });
  });

  const btnRefreshModels = document.getElementById("btn-refresh-models");
  if (btnRefreshModels) {
    btnRefreshModels.addEventListener("click", async function () {
      showStatus("無料モデル一覧を更新しています...");
      try {
        const res = await fetch("/api/models/refresh", { method: "POST" });
        if (!res.ok) throw new Error("無料モデル一覧の更新に失敗しました");
        showStatus("無料モデル一覧を更新しました。");
        window.location.reload();
      } catch (e) {
        showStatus(e.message, true);
      }
    });
  }

  const selectLlmMode = document.getElementById("select-llm-mode");
  const selectPreferredModel = document.getElementById("select-preferred-model");
  const toggleLlmEnabled = document.getElementById("toggle-llm-enabled");
  const toggleAutoSwitch = document.getElementById("toggle-auto-switch");

  function filterModelOptionsByMode() {
    if (!selectLlmMode || !selectPreferredModel) return;
    const mode = selectLlmMode.value;
    Array.from(selectPreferredModel.options).forEach((opt) => {
      if (!opt.value) {
        opt.hidden = false;
        return;
      }
      const prov = opt.getAttribute("data-provider");
      opt.hidden = mode !== "auto" && prov !== mode;
    });
  }

  async function saveLlmSelection() {
    if (!selectLlmMode) return;
    const payload = {
      selection_mode: selectLlmMode.value || "auto",
      preferred_model_id: selectPreferredModel?.value || null,
      llm_enabled: Boolean(toggleLlmEnabled?.checked),
      auto_switch_enabled: Boolean(toggleAutoSwitch?.checked),
    };
    try {
      const res = await fetch("/api/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      if (!res.ok) {
        const err = await res.json();
        throw new Error(err.detail || "LLM設定の保存に失敗しました");
      }
      showStatus("LLMモデル選択設定を保存しました。");
    } catch (e) {
      showStatus(e.message, true);
    }
  }

  if (selectLlmMode) {
    filterModelOptionsByMode();
    selectLlmMode.addEventListener("change", function () {
      filterModelOptionsByMode();
      saveLlmSelection();
    });
  }
  if (selectPreferredModel) {
    selectPreferredModel.addEventListener("change", saveLlmSelection);
  }
  if (toggleLlmEnabled) {
    toggleLlmEnabled.addEventListener("change", saveLlmSelection);
  }
  if (toggleAutoSwitch) {
    toggleAutoSwitch.addEventListener("change", saveLlmSelection);
  }

  const btnSaveLlmSelection = document.getElementById("btn-save-llm-selection");
  if (btnSaveLlmSelection) {
    btnSaveLlmSelection.addEventListener("click", saveLlmSelection);
  }

  const btnRetryPendingAi = document.getElementById("btn-retry-pending-ai");
  if (btnRetryPendingAi) {
    btnRetryPendingAi.addEventListener("click", async function () {
      const runId = btnRetryPendingAi.getAttribute("data-run-id") || null;
      btnRetryPendingAi.disabled = true;
      showStatus("保留中のAI処理を再開しています...");
      try {
        const res = await fetch("/api/news/retry-pending", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ run_id: runId }),
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || "保留中AI処理の再開に失敗しました");
        }
        window.location.reload();
      } catch (e) {
        showStatus(e.message, true);
        btnRetryPendingAi.disabled = false;
      }
    });
  }

  // --- 計測画面（metrics.html） ---
  const btnRunMetricsMeasurement = document.getElementById("btn-run-metrics-measurement");
  if (btnRunMetricsMeasurement) {
    btnRunMetricsMeasurement.addEventListener("click", async function () {
      btnRunMetricsMeasurement.disabled = true;
      showStatus("RSS取得量を計測しています（LLMは呼び出しません）...");
      try {
        const res = await fetch("/api/metrics/measure", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || "RSS取得量の計測に失敗しました");
        }
        showStatus("RSS取得量の計測が完了しました。");
        window.location.reload();
      } catch (e) {
        showStatus(e.message, true);
        btnRunMetricsMeasurement.disabled = false;
      }
    });
  }

  // --- 設定画面（settings.html） ---
  const btnSaveSettings = document.getElementById("btn-save-settings");
  if (btnSaveSettings) {
    btnSaveSettings.addEventListener("click", async function () {
      const displayCount = parseInt(document.getElementById("setting-display-count")?.value || "15", 10);
      const summaryLimit = parseInt(document.getElementById("setting-summary-char-limit")?.value || "200", 10);
      const autoTime = document.getElementById("setting-auto-update-time")?.value || "07:00";
      const dailyLimit = parseInt(document.getElementById("setting-daily-call-limit")?.value || "200", 10);
      const selectedCats = getCheckedValues("setting_selected_categories");
      const excludedCats = getCheckedValues("setting_excluded_categories");

      try {
        const res = await fetch("/api/settings", {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            display_count: displayCount,
            summary_char_limit: summaryLimit,
            auto_update_time: autoTime,
            daily_api_call_limit: dailyLimit,
            selected_categories: selectedCats,
            excluded_categories: excludedCats,
          }),
        });
        if (!res.ok) {
          const err = await res.json();
          throw new Error(err.detail || "設定の保存に失敗しました");
        }
        showStatus("設定を保存しました。");
      } catch (e) {
        showStatus(e.message, true);
      }
    });
  }
})();

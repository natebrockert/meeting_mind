import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createRoot } from "react-dom/client";
import "./styles.css";
import {
  Ital,
  LiveDot,
  Pill,
  SectionRule,
  Sidebar,
  SpeakerAvatar,
  SpeakerChip,
  type SidebarKey,
} from "./observatory";

// API DTOs and shared UI types live in src/types.ts (extracted in v0.1.4
// to keep this file manageable).
import type {
  ArchiveTimeline,
  AsrCandidateResult,
  Chapter,
  ChapterBullet,
  DashboardSettings,
  ExtractProgress,
  HealthStatus,
  InboxFile,
  IngestResponse,
  Meeting,
  MeetingDetail,
  MeetingOverview,
  OverlapHint,
  OwnerIdentity,
  OwnerSuggestion,
  PersonDetail,
  PersonSummary,
  Reflections,
  ReviewItem,
  ReviewTab,
  SchedulerJob,
  SearchResult,
  Segment,
  SetupItem,
  SetupStatus,
  SpeakerEvidence,
  SpeakerSuggestion,
  SynthesisSnapshot,
  TemplateOption,
  TranscriptCandidate,
  VersionInfo,
  WaveformData,
  WorkstreamIntelligence,
} from "./types";
import type { SettingsDraft } from "./types";

const dailyMaintenanceJobTypes = new Set(["health_check", "vault_lint", "action_rollup_rebuild"]);

import { api, isNetworkFailureMessage } from "./api";
import {
  getActiveAudio,
  pauseActiveAudio,
  playExactAudioSpan,
  setActiveAudio,
} from "./audio";
import { ConfirmModal } from "./components/ConfirmModal";
import { ConversationDriversPanel } from "./components/ConversationDriversPanel";
import { ForYouSection } from "./components/ForYouSection";
import { MeetingHealthChips } from "./components/MeetingHealthChips";
import { ReflectionsPanel } from "./components/ReflectionsPanel";
import { TranscriptRow } from "./components/TranscriptRow";
import { Waveform } from "./components/Waveform";
import {
  formatBytes,
  formatConfidence,
  formatDate,
  formatDateTime,
  formatMs,
  formatSeconds,
  formatTime,
  truncate,
} from "./format";

// ── App ────────────────────────────────────────────────────────────────────
function App() {
  const [mode, setMode] = useState<"night" | "day">(() => {
    const stored = localStorage.getItem("mm-mode");
    return stored === "day" ? "day" : "night";
  });
  const [activeView, setActiveView] = useState<SidebarKey>("review");
  const [reviewTab, setReviewTab] = useState<ReviewTab>("summary");
  const [transcriptMode, setTranscriptMode] = useState(false);

  const [meetings, setMeetings] = useState<Meeting[]>([]);
  const [selectedMeetingId, setSelectedMeetingId] = useState<number | null>(null);
  const [detail, setDetail] = useState<MeetingDetail | null>(null);
  const [query, setQuery] = useState("");
  const [rawMessage, setRawMessage] = useState("");
  const [versionInfo, setVersionInfo] = useState<VersionInfo | null>(null);
  const [upgradeModalOpen, setUpgradeModalOpen] = useState(false);
  const [upgrading, setUpgrading] = useState(false);
  // True while the backend is intentionally bouncing (restart or upgrade).
  // Network-failure toasts during this window are expected — squelch them
  // so the user doesn't see "Failed to fetch" pop up while their action
  // is actually succeeding.
  const [backendBouncing, setBackendBouncing] = useState(false);

  // setMessage: filters expected network-failure messages during a known
  // restart / upgrade window; anything else passes through unchanged.
  //
  // useCallback'd with empty deps so handlers that close over setMessage
  // (correctSegment, refreshAfterSegmentRevert, etc.) get stable identity
  // and don't bust their own memoization. A ref carries `backendBouncing`
  // so the callback stays stable while still reading the latest flag.
  const backendBouncingRef = useRef(false);
  backendBouncingRef.current = backendBouncing;
  const setMessage = useCallback((next: string) => {
    if (backendBouncingRef.current && isNetworkFailureMessage(next)) return;
    setRawMessage(next);
  }, []);
  const message = rawMessage;
  const [schedulerJobs, setSchedulerJobs] = useState<SchedulerJob[]>([]);
  const [maintenanceTime, setMaintenanceTime] = useState("02:00");
  const [speakerEditSegment, setSpeakerEditSegment] = useState<Segment | null>(null);
  const [health, setHealth] = useState<HealthStatus | null>(null);
  const [settings, setSettings] = useState<DashboardSettings | null>(null);
  const [settingsDraft, setSettingsDraft] = useState<SettingsDraft | null>(null);
  const settingsErrors = useMemo(() => validateSettingsDraft(settingsDraft), [settingsDraft]);
  const [crossMeetingQuery, setCrossMeetingQuery] = useState("");
  const [searchResults, setSearchResults] = useState<SearchResult[]>([]);
  const [activeSegmentId, setActiveSegmentId] = useState<number | null>(null);
  const [workstreamIntel, setWorkstreamIntel] = useState<WorkstreamIntelligence[]>([]);
  const [inboxFiles, setInboxFiles] = useState<InboxFile[]>([]);
  const [uploading, setUploading] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<{
    name: string;
    loaded: number;
    total: number;
  } | null>(null);
  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const nameSuggestionConfidenceThreshold = 0.55;
  const [people, setPeople] = useState<PersonSummary[]>([]);
  const [selectedPersonId, setSelectedPersonId] = useState<number | null>(null);
  const [personDetail, setPersonDetail] = useState<PersonDetail | null>(null);
  const [timeline, setTimeline] = useState<ArchiveTimeline | null>(null);
  const [templates, setTemplates] = useState<TemplateOption[]>([]);
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [extractProgress, setExtractProgress] = useState<ExtractProgress | null>(null);
  const [owner, setOwner] = useState<OwnerIdentity>({
    configured: false,
    person_id: null,
    display_name: null,
    aliases: [],
  });
  const [ownerSuggestion, setOwnerSuggestion] = useState<OwnerSuggestion | null>(null);
  // First-launch onboarding flow. Replaces the persistent "Is this you?"
  // banner; opens automatically when the user has no identity AND hasn't
  // dismissed it before. Re-openable from the sidebar identity chip.
  const [onboardingOpen, setOnboardingOpen] = useState(false);
  const [setupStatus, setSetupStatus] = useState<SetupStatus | null>(null);
  // Per-file template chosen on the Inbox screen before ingest fires.
  // Maps filename → template id. Files without an entry block the bulk
  // Ingest button. Direct uploads go through the IngestTemplateModal
  // instead, which sets the template on the upload request.
  const [inboxFileTemplates, setInboxFileTemplates] = useState<Record<string, string>>({});
  // Pending upload + template-pick modal: when the user drops a file, we
  // queue it here and ask for the template before firing the upload.
  const [pendingUpload, setPendingUpload] = useState<File | null>(null);
  // Pending delete + custom-confirm modal: replaces the native
  // window.confirm so the dialog matches the rest of the dashboard.
  const [deletePrompt, setDeletePrompt] = useState<{
    meetingId: number;
    title: string;
  } | null>(null);
  // Generic confirm-prompt slot so smaller destructive flows (delete a
  // generated workstream, revert a transcript segment) can reuse the
  // themed ConfirmModal instead of falling back to window.confirm.
  const [confirmPrompt, setConfirmPrompt] = useState<{
    title: string;
    body: React.ReactNode;
    confirmLabel: string;
    confirmTone?: "primary" | "danger";
    onConfirm: () => void;
  } | null>(null);
  // Post-ingest speaker-assignment modal: opens the first time the user
  // lands on a freshly-ingested meeting that still has unconfirmed
  // speakers. Dismissed meetings live in sessionStorage so the modal
  // stays out of the way for the rest of the session.
  const [postIngestSpeakerMeeting, setPostIngestSpeakerMeeting] = useState<
    number | null
  >(null);
  const [postIngestSeen, setPostIngestSeen] = useState<Set<number>>(() => {
    try {
      const raw = sessionStorage.getItem("mm-post-ingest-seen");
      return new Set(raw ? (JSON.parse(raw) as number[]) : []);
    } catch {
      return new Set();
    }
  });
  const markPostIngestSeen = (meetingId: number) => {
    setPostIngestSeen((prev) => {
      const next = new Set(prev);
      next.add(meetingId);
      try {
        sessionStorage.setItem("mm-post-ingest-seen", JSON.stringify(Array.from(next)));
      } catch {
        /* ignore */
      }
      return next;
    });
  };
  const [recentPaletteIds, setRecentPaletteIds] = useState<string[]>(() => {
    try {
      const raw = localStorage.getItem("mm-palette-recents");
      return raw ? (JSON.parse(raw) as string[]) : [];
    } catch {
      return [];
    }
  });
  const extractStreamRef = useRef<EventSource | null>(null);
  useEffect(
    () => () => {
      // Always tear down the SSE on unmount, even if an in-flight extract
      // never resolved (component unmounted before the finally block ran).
      extractStreamRef.current?.close();
      extractStreamRef.current = null;
    },
    [],
  );

  useEffect(() => {
    document.documentElement.setAttribute("data-mm-mode", mode);
    localStorage.setItem("mm-mode", mode);
  }, [mode]);

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      // Don't hijack ⌘K while the user is typing in an input/textarea;
      // they almost certainly mean to type the literal "k".
      const target = event.target as HTMLElement | null;
      const tag = target?.tagName;
      const isEditable =
        tag === "INPUT" ||
        tag === "TEXTAREA" ||
        target?.getAttribute("contenteditable") === "true";
      if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
        if (isEditable) return;
        event.preventDefault();
        setPaletteOpen((open) => !open);
      } else if (event.key === "Escape" && paletteOpen) {
        setPaletteOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [paletteOpen]);

  const refreshMeetings = async () => {
    const data = await api.get<{ meetings: Meeting[] }>("/api/meetings");
    setMeetings(data.meetings);
    if (!selectedMeetingId && data.meetings[0]) setSelectedMeetingId(data.meetings[0].id);
  };

  const refreshScheduler = async () => {
    const data = await api.get<{ jobs: SchedulerJob[] }>("/api/scheduler/jobs");
    setSchedulerJobs(data.jobs);
  };

  const refreshWorkstreamIntel = async () => {
    const data = await api.get<{ workstreams: WorkstreamIntelligence[] }>("/api/workstreams/intelligence");
    setWorkstreamIntel(data.workstreams);
  };

  const refreshInbox = async () => {
    const data = await api.get<{ files: InboxFile[] }>("/api/inbox");
    setInboxFiles(data.files);
  };

  const refreshPeople = async () => {
    try {
      const data = await api.get<{ people: PersonSummary[] }>("/api/people");
      setPeople(data.people);
    } catch {
      setPeople([]);
    }
  };

  const refreshTimeline = async () => {
    try {
      const data = await api.get<ArchiveTimeline>("/api/timeline?weeks=16");
      setTimeline(data);
    } catch {
      setTimeline(null);
    }
  };

  const refreshTemplates = async () => {
    try {
      const data = await api.get<{ templates: TemplateOption[] }>("/api/templates");
      setTemplates(data.templates);
    } catch {
      // pre-existing backend without templates support — fall back silently
      setTemplates([{ id: "general", name: "General" }]);
    }
  };

  const refreshOwner = async () => {
    try {
      const data = await api.get<OwnerIdentity>("/api/owner");
      setOwner(data);
      if (!data.configured) {
        try {
          const sug = await api.get<{ suggestion: OwnerSuggestion | null }>(
            "/api/owner/suggest",
          );
          setOwnerSuggestion(sug.suggestion);
        } catch {
          setOwnerSuggestion(null);
        }
        // Auto-open onboarding once per install. The user can dismiss to
        // skip; subsequent loads stay quiet until they reopen from the
        // sidebar identity chip.
        if (!localStorage.getItem("mm-onboarding-dismissed")) {
          setOnboardingOpen(true);
        }
      } else {
        setOwnerSuggestion(null);
      }
    } catch {
      // pre-existing backend without owner support — silent
    }
  };

  const saveOwner = async (
    personId: number | null,
    displayName: string | null,
    aliases: string[],
  ) => {
    const params = new URLSearchParams();
    if (personId !== null) params.set("person_id", String(personId));
    if (displayName) params.set("display_name", displayName);
    if (aliases.length) params.set("aliases", aliases.join(","));
    await api.post(`/api/owner?${params.toString()}`);
    await refreshOwner();
    await refreshSetupStatus();
    if (selectedMeetingId) {
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
    }
    setMessage(`Set owner to ${displayName || `person ${personId}`}.`);
  };

  const clearOwnerIdentity = async () => {
    await api.delete("/api/owner");
    await refreshOwner();
    await refreshSetupStatus();
    setMessage("Cleared owner identity.");
  };

  const pushRecentPaletteAction = (key: string) => {
    setRecentPaletteIds((current) => {
      const next = [key, ...current.filter((id) => id !== key)].slice(0, 8);
      try {
        localStorage.setItem("mm-palette-recents", JSON.stringify(next));
      } catch {
        // localStorage may be unavailable in some embedded contexts
      }
      return next;
    });
  };

  const loadPersonDetail = async (personId: number) => {
    setSelectedPersonId(personId);
    try {
      const data = await api.get<PersonDetail>(`/api/people/${personId}`);
      setPersonDetail(data);
    } catch (error) {
      setPersonDetail(null);
      setMessage(error instanceof Error ? error.message : "Person lookup failed.");
    }
  };

  const setMeetingTemplate = async (meetingId: number, templateId: string) => {
    try {
      await api.post(
        `/api/meetings/${meetingId}/template?template=${encodeURIComponent(templateId)}`,
      );
      setMessage(`Template set to ${templateId}. Re-extracting…`);
      try {
        await api.post(`/api/meetings/${meetingId}/extract`);
        setDetail(await api.get<MeetingDetail>(`/api/meetings/${meetingId}`));
        setMessage(`Template set to ${templateId}. Re-extraction complete.`);
      } catch (extractError) {
        setMessage(
          extractError instanceof Error
            ? `Template updated, but re-extraction failed: ${extractError.message}`
            : "Template updated, but re-extraction failed.",
        );
      }
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Template update failed.");
    }
  };

  // Pulled out so the owner-save path can re-fetch setup status too.
  // Without this, setting the identity from the onboarding wizard
  // updates the owner chip but leaves the Inbox checklist showing
  // "Your identity set" as undone — which then gates Ingest behind
  // a stale precondition. Identity-state changes need to invalidate
  // setup-status the same way settings changes do.
  const refreshSetupStatus = async () => {
    try {
      setSetupStatus(await api.get<SetupStatus>("/api/setup-status"));
    } catch {
      setSetupStatus(null);
    }
  };

  const refreshSettings = async () => {
    const data = await api.get<DashboardSettings>("/api/settings");
    setSettings(data);
    setSettingsDraft(settingsToDraft(data));
    await refreshSetupStatus();
  };

  const refreshVersionInfo = async () => {
    try {
      setVersionInfo(await api.get<VersionInfo>("/api/system/version"));
    } catch {
      setVersionInfo(null);
    }
  };

  useEffect(() => {
    void api.post("/api/install").then(async () => {
      try {
        // These nine fetches are independent of each other — fanning them
        // out in parallel cuts startup wait time noticeably (~1.5-3s on
        // typical local networks). Health stays first so the error path
        // can surface backend-down before the rest fail in confusing ways.
        setHealth(await api.get<HealthStatus>("/api/health"));
        await Promise.all([
          refreshSettings(),
          refreshMeetings(),
          refreshScheduler(),
          refreshWorkstreamIntel(),
          refreshInbox(),
          refreshPeople(),
          refreshTimeline(),
          refreshTemplates(),
          refreshOwner(),
        ]);
        void refreshVersionInfo();
      } catch (error) {
        setMessage(error instanceof Error ? error.message : "Failed to load initial state.");
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    if (!selectedMeetingId) return;
    // Cancel against rapid meeting switches so a late response from a prior
    // meeting can't overwrite the current one. Also clear any stale detail
    // tied to a previous selection so the UI doesn't show the wrong meeting
    // briefly while the new fetch is in flight.
    let cancelled = false;
    setDetail((current) =>
      current && current.meeting.id !== selectedMeetingId ? null : current,
    );
    void api
      .get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`)
      .then((next) => {
        if (!cancelled) setDetail(next);
      })
      .catch((error) => {
        if (cancelled) return;
        // Surface the failure to the user AND fall back to the meetings
        // list so they can pick a different meeting. The previous
        // implementation silently swallowed the rejection, which is what
        // made "click review then a meeting → blank screen" feel like a
        // load bug.
        const detailMsg = error instanceof Error ? error.message : "Failed to load meeting.";
        setMessage(detailMsg);
        if (detailMsg.toLowerCase().includes("not found")) {
          setSelectedMeetingId(null);
          setDetail(null);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [selectedMeetingId]);

  useEffect(() => {
    if (!transcriptMode || !activeSegmentId) return;
    window.requestAnimationFrame(() => {
      document.getElementById(`segment-${activeSegmentId}`)?.scrollIntoView({
        block: "center",
        behavior: "smooth",
      });
    });
  }, [activeSegmentId, detail, transcriptMode]);

  useEffect(() => {
    const schedule = schedulerJobs.find((job) => job.schedule.includes(":"))?.schedule || "";
    const match = schedule.match(/(\d{2}:\d{2})/);
    if (match) setMaintenanceTime(match[1]);
  }, [schedulerJobs]);

  const subscribeExtractProgress = (meetingId: number): EventSource | null => {
    if (typeof EventSource === "undefined") return null;
    extractStreamRef.current?.close();
    const source = new EventSource(`/api/meetings/${meetingId}/extract/stream`);
    extractStreamRef.current = source;
    source.onmessage = (event) => {
      try {
        const payload = JSON.parse(event.data) as ExtractProgress;
        setExtractProgress(payload);
        if (payload.status === "complete" || payload.status === "failed" || payload.status === "timeout") {
          source.close();
          if (extractStreamRef.current === source) extractStreamRef.current = null;
          window.setTimeout(() => setExtractProgress(null), 2000);
        }
      } catch {
        // ignore malformed frame
      }
    };
    source.onerror = () => {
      source.close();
      if (extractStreamRef.current === source) extractStreamRef.current = null;
      setExtractProgress(null);
    };
    return source;
  };

  // v0.2.10: auto-resubscribe to in-flight pipeline progress on page
  // reload/navigation so the progress bar reappears after a refresh.
  //
  // Heuristic: a meeting whose status is `ingested` is still being
  // processed. `transcribed` USED to mean "in flight too" but v0.2.15
  // added an `extracted` terminal status, so meetings stuck at
  // `transcribed` indefinitely (e.g. older meetings ingested before
  // the v0.2.15 fix) would otherwise replay a stale "extract
  // complete 100%" toast on every reload. Now we only resubscribe on
  // truly mid-flight statuses.
  useEffect(() => {
    if (extractStreamRef.current) return;
    const inflight = meetings.find((m) => m.status === "ingested");
    if (!inflight) return;
    subscribeExtractProgress(inflight.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meetings]);

  const finishPipelineForIngest = async (data: IngestResponse) => {
    const meetingIds = data.results
      .map((result) => result.meeting_id)
      .filter((meetingId): meetingId is number => Number.isInteger(meetingId));
    if (!meetingIds.length) {
      setMessage(`Ingested ${data.results.length} file(s). No new supported recordings to process.`);
      return;
    }
    setMessage(`Processing ${meetingIds.length} recording(s). This can take several minutes.`);
    for (const meetingId of meetingIds) {
      const stream = subscribeExtractProgress(meetingId);
      try {
        await api.post(`/api/meetings/${meetingId}/process`);
        await api.post(`/api/meetings/${meetingId}/extract`);
      } finally {
        stream?.close();
      }
    }
    setExtractProgress(null);
    await refreshMeetings();
    await refreshInbox();
    await refreshWorkstreamIntel();
    setSelectedMeetingId(meetingIds[0]);
    setDetail(await api.get<MeetingDetail>(`/api/meetings/${meetingIds[0]}`));
    setActiveView("review");
    setTranscriptMode(false);
    setReviewTab("summary");
    setMessage(`Processed ${meetingIds.length} recording(s). Review the generated meeting note.`);
  };

  const ingest = async () => {
    try {
      const templatesPayload = JSON.stringify(inboxFileTemplates);
      const data = await api.post<IngestResponse>(
        `/api/ingest?templates_json=${encodeURIComponent(templatesPayload)}`,
      );
      // Clear the local template map: filenames are gone from the
      // inbox after ingest, so retaining entries is dead state.
      setInboxFileTemplates({});
      await finishPipelineForIngest(data);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Ingest failed.");
    }
  };

  // Open the template-pick modal instead of firing upload immediately —
  // user must choose a meeting type before the file is transcribed.
  const queueUpload = (file: File) => {
    setPendingUpload(file);
  };

  const runQueuedUpload = async (template: string) => {
    const file = pendingUpload;
    setPendingUpload(null);
    if (!file) return;
    setUploading(true);
    setUploadProgress({ name: file.name, loaded: 0, total: file.size });
    try {
      const data = await api.upload<IngestResponse>(
        `/api/upload?template=${encodeURIComponent(template)}`,
        file,
        (loaded, total) => setUploadProgress({ name: file.name, loaded, total }),
      );
      await finishPipelineForIngest(data);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Upload failed.");
    } finally {
      setUploading(false);
      setUploadProgress(null);
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  };

  const runStage = async () => {
    if (!selectedMeetingId) return;
    const data = await api.post<{ path: string }>(`/api/meetings/${selectedMeetingId}/stage`);
    setMessage(`Staged: ${data.path}`);
  };

  const deleteWorkstream = (displayName: string) => {
    setConfirmPrompt({
      title: `Remove "${displayName}"?`,
      body: (
        <>
          <p style={{ margin: 0 }}>
            Removes this generated workstream from matching meetings and
            refreshes promoted vault notes where possible.
          </p>
          <p style={{ marginTop: 8, fontSize: 12, opacity: 0.75 }}>
            Your audio + transcript are untouched. You can re-run extraction
            to bring the workstream back.
          </p>
        </>
      ),
      confirmLabel: "✕ Remove workstream",
      confirmTone: "danger",
      onConfirm: async () => {
        setConfirmPrompt(null);
        try {
          const data = await api.delete<{ removed: number }>(
            `/api/workstreams?title=${encodeURIComponent(displayName)}`,
          );
          setMessage(`Removed ${data.removed} generated workstream item(s).`);
          await refreshWorkstreamIntel();
          if (selectedMeetingId) {
            setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
          }
        } catch (error) {
          setMessage(error instanceof Error ? error.message : "Workstream delete failed.");
        }
      },
    });
  };

  const runPromote = async () => {
    if (!selectedMeetingId) return;
    if (!readyForPromotion) {
      setMessage("Promotion is blocked until speaker review is complete.");
      return;
    }
    if (detail?.overview?.status === "promoted") {
      setMessage("This meeting is already promoted.");
      return;
    }
    try {
      const data = await api.post<{ path: string }>(`/api/meetings/${selectedMeetingId}/promote`);
      setMessage(`Promoted: ${data.path}`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Promotion failed.");
    }
  };

  const exportPdf = () => {
    if (!selectedMeetingId) return;
    window.open(`/api/meetings/${selectedMeetingId}/pdf`, "_blank", "noopener,noreferrer");
  };

  const exportHtml = () => {
    if (!selectedMeetingId) return;
    window.open(`/api/meetings/${selectedMeetingId}/html`, "_blank", "noopener,noreferrer");
  };

  const requestDeleteMeeting = (meetingId: number, title: string) => {
    setDeletePrompt({ meetingId, title });
  };

  const confirmDeleteMeeting = async () => {
    const prompt = deletePrompt;
    setDeletePrompt(null);
    if (!prompt) return;
    const { meetingId, title } = prompt;
    try {
      await api.delete(`/api/meetings/${meetingId}`);
      setMessage(`Deleted meeting "${title}".`);
      if (selectedMeetingId === meetingId) {
        setSelectedMeetingId(null);
        setDetail(null);
        setTranscriptMode(false);
      }
      await refreshMeetings();
      await refreshWorkstreamIntel();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Delete failed.");
    }
  };

  const deleteInboxFile = (file: InboxFile) => {
    setConfirmPrompt({
      title: `Remove "${file.name}" from the inbox?`,
      body: (
        <p style={{ margin: 0 }}>
          The audio file will be deleted from disk. This can't be undone.
        </p>
      ),
      confirmLabel: "✕ Remove file",
      confirmTone: "danger",
      onConfirm: async () => {
        setConfirmPrompt(null);
        try {
          await api.delete(`/api/inbox?path=${encodeURIComponent(file.path)}`);
          setMessage(`Removed ${file.name} from inbox.`);
          await refreshInbox();
        } catch (error) {
          setMessage(error instanceof Error ? error.message : "Inbox delete failed.");
        }
      },
    });
  };

  const renameWorkstream = async (oldName: string, newName: string) => {
    const cleanNew = newName.trim();
    if (!cleanNew || cleanNew.toLowerCase() === oldName.toLowerCase()) return;
    try {
      await api.put(
        `/api/workstreams?title=${encodeURIComponent(oldName)}&new_title=${encodeURIComponent(cleanNew)}`,
      );
      setMessage(`Renamed "${oldName}" → "${cleanNew}".`);
      await refreshWorkstreamIntel();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Rename failed.");
    }
  };

  const approveSpeakerName = async (speakerId: string, label: string, successMessage: string) => {
    if (!selectedMeetingId) return;
    const cleanLabel = label.trim();
    if (!cleanLabel) {
      setMessage("Speaker name cannot be blank.");
      return;
    }
    const path = `/api/meetings/${selectedMeetingId}/speakers/${encodeURIComponent(speakerId)}/approve`;
    try {
      await api.post(`${path}?label=${encodeURIComponent(cleanLabel)}`);
      setMessage(successMessage);
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Speaker update failed.");
    }
  };

  // Audit (v0.1.5): TranscriptRow is memo'd, so the handlers it receives
  // need stable identity. Without `useCallback` they'd be fresh every
  // App render and `React.memo` would short-circuit nothing.
  const correctSegment = useCallback(
    async (segment: Segment, correctedText: string) => {
      if (!selectedMeetingId) return;
      const corrected = correctedText.trim();
      if (!corrected || corrected === segment.text) return;
      const path = `/api/meetings/${selectedMeetingId}/segments/${segment.id}/correct`;
      await api.post(`${path}?corrected_text=${encodeURIComponent(corrected)}&reason=manual_review`);
      setMessage(`Updated transcript row at ${formatMs(segment.start_ms)}.`);
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
    },
    [selectedMeetingId],
  );

  const refreshAfterSegmentRevert = useCallback(async () => {
    if (!selectedMeetingId) return;
    setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
  }, [selectedMeetingId]);

  // v0.2.8: accept/reject speaker re-attribution proposals from Pass C.
  // Backend endpoints update transcript_segments via the existing
  // reassign_segment_speaker path AND mark the review_item resolved.
  // v0.2.10 Pass D: accept/reject segment-split proposals. The backend
  // shrinks the head segment, inserts a new tail with the proposed
  // speaker, and reattributes words at/after the split timestamp.
  const acceptSplit = useCallback(
    async (reviewItemId: number) => {
      if (!selectedMeetingId) return;
      try {
        await api.post(
          `/api/meetings/${selectedMeetingId}/review-items/${reviewItemId}/accept-split`,
        );
        setMessage("Split applied.");
        setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      } catch (error) {
        setMessage(
          error instanceof Error ? error.message : "Couldn't apply the split.",
        );
      }
    },
    [selectedMeetingId, setMessage],
  );
  const rejectSplit = useCallback(
    async (reviewItemId: number) => {
      if (!selectedMeetingId) return;
      try {
        await api.post(
          `/api/meetings/${selectedMeetingId}/review-items/${reviewItemId}/reject-split`,
        );
        setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      } catch (error) {
        setMessage(
          error instanceof Error ? error.message : "Couldn't dismiss the split.",
        );
      }
    },
    [selectedMeetingId, setMessage],
  );

  const acceptReattribution = useCallback(
    async (reviewItemId: number, proposedSpeaker: string) => {
      if (!selectedMeetingId) return;
      try {
        await api.post(
          `/api/meetings/${selectedMeetingId}/review-items/${reviewItemId}/accept-reattribution`,
        );
        setMessage(`Updated speaker to ${proposedSpeaker}.`);
        setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      } catch (error) {
        setMessage(
          error instanceof Error
            ? error.message
            : "Couldn't apply the speaker correction.",
        );
      }
    },
    [selectedMeetingId, setMessage],
  );
  const rejectReattribution = useCallback(
    async (reviewItemId: number) => {
      if (!selectedMeetingId) return;
      try {
        await api.post(
          `/api/meetings/${selectedMeetingId}/review-items/${reviewItemId}/reject-reattribution`,
        );
        setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      } catch (error) {
        setMessage(
          error instanceof Error ? error.message : "Couldn't dismiss the suggestion.",
        );
      }
    },
    [selectedMeetingId, setMessage],
  );

  const renameSpeaker = async (speakerId: string, label: string) => {
    if (!selectedMeetingId) return;
    const alias = speakerAliasById.get(speakerId) || speakerId;
    await approveSpeakerName(speakerId, label, `Saved ${alias} as ${label.trim()}.`);
    setSpeakerEditSegment(null);
  };

  const moveSegmentSpeaker = async (segment: Segment, targetSpeakerId: string) => {
    if (!selectedMeetingId) return;
    const targetName = speakerDisplayNameById.get(targetSpeakerId) || speakerAliasById.get(targetSpeakerId) || targetSpeakerId;
    try {
      const path = `/api/meetings/${selectedMeetingId}/segments/${segment.id}/reassign`;
      await api.post(`${path}?speaker_id=${encodeURIComponent(targetSpeakerId)}`);
      setMessage(`Moved this card to ${targetName}.`);
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      setSpeakerEditSegment(null);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Speaker reassignment failed.");
    }
  };

  const moveAllSpeakerSegments = async (sourceSpeakerId: string, targetSpeakerId: string) => {
    if (!selectedMeetingId) return;
    const sourceName = speakerDisplayNameById.get(sourceSpeakerId) || speakerAliasById.get(sourceSpeakerId) || sourceSpeakerId;
    const targetName = speakerDisplayNameById.get(targetSpeakerId) || speakerAliasById.get(targetSpeakerId) || targetSpeakerId;
    try {
      const path = `/api/meetings/${selectedMeetingId}/speakers/${encodeURIComponent(sourceSpeakerId)}/reassign-all`;
      await api.post(`${path}?target_speaker_id=${encodeURIComponent(targetSpeakerId)}`);
      setMessage(`Moved all ${sourceName} cards to ${targetName}.`);
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      setSpeakerEditSegment(null);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Speaker reassignment failed.");
    }
  };

  const saveSummary = async (summary: string) => {
    if (!selectedMeetingId) return;
    const cleanSummary = summary.trim();
    if (!cleanSummary) {
      setMessage("Summary cannot be blank.");
      return;
    }
    try {
      await api.post(`/api/meetings/${selectedMeetingId}/summary?summary=${encodeURIComponent(cleanSummary)}`);
      setMessage("Updated summary.");
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Summary update failed.");
    }
  };

  const configureDailyMaintenance = async (enabled: boolean) => {
    try {
      await api.post(
        `/api/scheduler/daily-maintenance?enabled=${enabled ? "true" : "false"}&run_time=${encodeURIComponent(maintenanceTime)}`,
      );
      setMessage(enabled ? `Daily maintenance will run at ${maintenanceTime}.` : "Daily maintenance disabled.");
      await refreshScheduler();
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Scheduler update failed.");
    }
  };

  const saveSettings = async () => {
    if (!settingsDraft) return;
    if (settingsErrors.length) {
      setMessage(`Fix settings before saving: ${settingsErrors.join(" ")}`);
      return;
    }
    try {
      const vocabularyTerms = settingsDraft.vocabularyTerms
        .split(/\r?\n|,/)
        .map((term) => term.trim())
        .filter(Boolean);
      const data = await api.postJson<{ settings: DashboardSettings }>("/api/settings", {
        dashboard_port: Number(settingsDraft.dashboardPort),
        backend_port: Number(settingsDraft.backendPort),
        model_provider: settingsDraft.modelProvider,
        default_model: settingsDraft.defaultModel,
        quality_model: settingsDraft.qualityModel,
        lm_studio_base_url: settingsDraft.lmStudioBaseUrl,
        ollama_base_url: settingsDraft.ollamaBaseUrl,
        model_idle_ttl_seconds: Number(settingsDraft.modelIdleTtlSeconds),
        model_temperature: Number(settingsDraft.modelTemperature),
        auto_audio_repair: settingsDraft.autoAudioRepair,
        vocal_presentation_cue_scoring: settingsDraft.vocalPresentationCueScoring,
        show_key_term_highlights: settingsDraft.showKeyTermHighlights,
        show_transcript_confidence_chips: settingsDraft.showTranscriptConfidenceChips,
        default_template: settingsDraft.defaultTemplate,
        auto_send_to_obsidian: settingsDraft.autoSendToObsidian,
        asr_vocabulary_terms: vocabularyTerms,
      });
      setSettings(data.settings);
      setSettingsDraft(settingsToDraft(data.settings));
      setHealth(await api.get<HealthStatus>("/api/health"));
      setMessage("Settings saved to config/local.toml. Port changes require restarting the dashboard/backend.");
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Settings update failed.");
    }
  };

  const runAudioAlternatives = async () => {
    if (!selectedMeetingId) return;
    try {
      setMessage("Checking low-confidence audio spans. This can take a few minutes.");
      const data = await api.post<{ candidates: AsrCandidateResult[] }>(
        `/api/meetings/${selectedMeetingId}/asr-candidates?limit=4`,
      );
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      setMessage(
        data.candidates.length
          ? `Generated ${data.candidates.length} audio alternative(s). Review and accept only if better.`
          : "No audio alternatives were generated for the current low-confidence spans.",
      );
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Audio alternatives failed.");
    }
  };

  const acceptAudioAlternative = async (candidate: TranscriptCandidate) => {
    if (!selectedMeetingId) return;
    try {
      await api.post(`/api/meetings/${selectedMeetingId}/asr-candidates/${candidate.id}/accept`);
      setDetail(await api.get<MeetingDetail>(`/api/meetings/${selectedMeetingId}`));
      setMessage(`Accepted audio alternative for ${formatMs(candidate.start_ms)}.`);
    } catch (error) {
      setMessage(error instanceof Error ? error.message : "Audio alternative accept failed.");
    }
  };

  const runCrossMeetingSearch = async () => {
    const queryText = crossMeetingQuery.trim();
    if (!queryText) {
      setSearchResults([]);
      return;
    }
    const data = await api.get<{ results: SearchResult[] }>(
      `/api/search?q=${encodeURIComponent(queryText)}&limit=40`,
    );
    setSearchResults(data.results);
  };

  const openSearchResult = (result: SearchResult) => {
    setSelectedMeetingId(result.meeting_id);
    setActiveView("review");
    setTranscriptMode(true);
    setReviewTab("transcript");
    setActiveSegmentId(result.segment_id || result.source_segment_ids[0] || null);
    setQuery(crossMeetingQuery.trim());
  };

  const speakerIds = useMemo(() => orderedSpeakerIds(detail?.segments || []), [detail]);
  const speakerAliasById = useMemo(() => speakerAliasMap(speakerIds), [speakerIds]);
  const speakerLabels = useMemo(
    () =>
      new Map(
        (detail?.assignments || []).map((assignment) => [
          assignment.diarization_speaker_id,
          assignment.approved_label || assignment.diarization_speaker_id,
        ]),
      ),
    [detail],
  );
  const savedSpeakerIds = useMemo(
    () => savedSpeakerIdSet(speakerIds, speakerAliasById, speakerLabels),
    [speakerIds, speakerAliasById, speakerLabels],
  );
  // Open the post-ingest speaker modal once per session per meeting when
  // there are voices but none are confirmed yet. The modal pulls suggested
  // names from the existing review_items so the user can pick from a
  // ranked list instead of starting blank.
  useEffect(() => {
    if (!detail) return;
    if (postIngestSpeakerMeeting === detail.meeting.id) return;
    if (postIngestSeen.has(detail.meeting.id)) return;
    if (speakerIds.length === 0) return;
    if (savedSpeakerIds.size >= speakerIds.length) return;
    setPostIngestSpeakerMeeting(detail.meeting.id);
  }, [detail, speakerIds, savedSpeakerIds, postIngestSpeakerMeeting, postIngestSeen]);
  const reviewItems = detail?.review_items || [];

  // v0.2.8: surface speaker re-attribution proposals (kind=speaker_reattribution).
  // Open proposals only — accepted/rejected ones stay in the DB for history
  // but shouldn't pile up in the review queue.
  const reattributionProposals = useMemo(
    () =>
      reviewItems
        .filter(
          (item) => item.kind === "speaker_reattribution" && item.status === "open",
        )
        .map((item) => {
          try {
            const payload = JSON.parse(item.payload_json || "{}");
            return {
              reviewItemId: item.id,
              segmentId: Number(payload.segment_id),
              currentSpeaker: String(payload.current_speaker || ""),
              proposedSpeaker: String(payload.proposed_speaker || ""),
              confidence: typeof item.confidence === "number" ? item.confidence : 0,
              basis: String(payload.basis || ""),
            };
          } catch {
            return null;
          }
        })
        .filter((p): p is NonNullable<typeof p> => p !== null && !!p.proposedSpeaker),
    [reviewItems],
  );

  // v0.2.10 Pass D: surface segment-split proposals (kind=segment_split_proposal).
  // Same lifecycle as reattribution proposals — open only, parsed from payload.
  const splitProposals = useMemo(
    () =>
      reviewItems
        .filter(
          (item) => item.kind === "segment_split_proposal" && item.status === "open",
        )
        .map((item) => {
          try {
            const payload = JSON.parse(item.payload_json || "{}");
            return {
              reviewItemId: item.id,
              segmentId: Number(payload.segment_id),
              splitAtMs: Number(payload.split_at_ms || 0),
              headText: String(payload.head_text || ""),
              tailText: String(payload.tail_text || ""),
              tailSpeakerId: String(payload.tail_speaker_id || ""),
              evidence: String(payload.evidence || ""),
              confidence: typeof item.confidence === "number" ? item.confidence : 0,
            };
          } catch {
            return null;
          }
        })
        .filter((p): p is NonNullable<typeof p> => p !== null && !!p.tailText),
    [reviewItems],
  );

  // v0.2.11: auto-applied repairs are intentionally NOT shown in the
  // meeting view. The pipeline applied them; the user is here to
  // read, not to be told what got done. The audit trail lives in
  // `review_items` with `status='auto_applied'` for any caller (CLI,
  // API, future ops surface) that wants to inspect.

  const speakerSuggestions = useMemo(
    () =>
      reviewItems
        .filter((item) => item.kind === "speaker_name_candidate" || item.kind === "speaker_profile_match")
        .map(parseSpeakerSuggestion)
        .filter((suggestion): suggestion is SpeakerSuggestion => Boolean(suggestion))
        .filter((suggestion) => !savedSpeakerIds.has(suggestion.speakerId)),
    [reviewItems, savedSpeakerIds],
  );
  const bestSpeakerSuggestionById = useMemo(
    () => bestSpeakerSuggestionMap(speakerSuggestions),
    [speakerSuggestions],
  );
  const speakerDisplayNameById = useMemo(
    () =>
      displaySpeakerNameMap(
        speakerIds,
        speakerAliasById,
        speakerLabels,
        bestSpeakerSuggestionById,
        nameSuggestionConfidenceThreshold,
      ),
    [speakerIds, speakerAliasById, speakerLabels, bestSpeakerSuggestionById],
  );
  const filteredSegments = useMemo(() => {
    const segments = detail?.segments || [];
    if (!query.trim()) return segments;
    const lower = query.toLowerCase();
    return segments.filter((segment) => {
      const speakerAlias = speakerAliasById.get(segment.diarization_speaker_id) || "";
      const speakerName = speakerDisplayNameById.get(segment.diarization_speaker_id) || "";
      return (
        segment.text.toLowerCase().includes(lower) ||
        segment.diarization_speaker_id.toLowerCase().includes(lower) ||
        speakerAlias.toLowerCase().includes(lower) ||
        speakerName.toLowerCase().includes(lower)
      );
    });
    // Audit perf MED: narrowed from `[detail, ...]` to just `detail?.segments`
    // so the filter doesn't recompute on unrelated detail-object changes
    // (review items, synthesis, etc.).
  }, [detail?.segments, query, speakerAliasById, speakerDisplayNameById]);
  const segmentById = useMemo(
    () => new Map((detail?.segments || []).map((segment) => [segment.id, segment])),
    [detail?.segments],
  );
  const summaryItem = reviewItems.find((item) => item.kind === "summary");
  const summaryText = detail?.synthesis.summary || (summaryItem ? parseSummaryText(summaryItem.payload_json) : "");
  const showKeyTermHighlights =
    settings?.dashboard_prefs?.show_key_term_highlights ?? false;
  const showConfidenceChips =
    settings?.dashboard_prefs?.show_transcript_confidence_chips ?? false;
  // Audit H1 (v0.1.6): without `useMemo`, the `|| []` and the ternary's
  // `: []` allocate fresh arrays every render. That fresh identity is
  // passed as `highlightTerms` to every TranscriptRow → React.memo sees
  // a "changed" prop on every parent render → memo is a no-op and the
  // row's `useMemo` around `highlightImportantText` also re-runs every
  // tick. Stable identity here is what makes the row's perf branch real.
  const keyTerms = useMemo(
    () => (showKeyTermHighlights ? detail?.synthesis.key_terms || [] : []),
    [showKeyTermHighlights, detail?.synthesis.key_terms],
  );
  const ownerTerms = useMemo(() => {
    if (!owner.configured) return [] as string[];
    const out: string[] = [];
    if (owner.display_name) out.push(owner.display_name);
    out.push(...owner.aliases);
    return out;
  }, [owner]);
  const readyForPromotion = Boolean(detail?.overview && detail.overview.speaker_status === "complete");
  const maintenanceEnabled = schedulerJobs.some(
    (job) => dailyMaintenanceJobTypes.has(job.job_type) && Boolean(job.enabled),
  );
  const maintenanceStatus = schedulerJobs
    .filter((job) => dailyMaintenanceJobTypes.has(job.job_type))
    .map((job) => job.last_status || "not run")
    .join(", ");

  const speakerNumberById = useMemo(() => {
    const map = new Map<string, number>();
    speakerIds.forEach((id, index) => map.set(id, index + 1));
    return map;
  }, [speakerIds]);
  const speakerNumberOf = (id: string) => speakerNumberById.get(id) || 1;

  // name→slot map used by renderMentions to color confirmed speaker
  // names inline with the transcript palette. Built once per meeting
  // and supplied via SpeakerNameProvider so every renderMentions
  // descendant picks it up without prop-drilling.
  const speakerNameSlots = useMemo<SpeakerNameSlots>(() => {
    return buildSpeakerNameSlots(speakerDisplayNameById, speakerNumberOf);
  // speakerNumberOf is a stable lookup over speakerNumberById; including
  // its source map is enough to recompute the slots on real changes.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [speakerDisplayNameById, speakerNumberById]);

  // Chapter index: derive from segments grouped by speaker shift, fall back to topics
  const chapters = useMemo(() => deriveChapters(detail), [detail]);

  return (
    <SpeakerNameProvider slots={speakerNameSlots}>
    <div className="mm-shell">
      <Sidebar
        active={activeView}
        onSelect={(key) => {
          setActiveView(key);
          if (key === "review") setTranscriptMode(false);
          if (key === "people") void refreshPeople();
          if (key === "archive") void refreshTimeline();
        }}
        inboxCount={inboxFiles.length}
        reviewCount={meetings.length}
        workstreamCount={workstreamIntel.length}
        peopleCount={people.length}
        mode={mode}
        onToggleMode={() => setMode(mode === "night" ? "day" : "night")}
        backendUrl={settings?.backend.url}
        onOpenPalette={() => setPaletteOpen(true)}
        ownerName={owner.configured ? owner.display_name : null}
        onOpenOnboarding={() => setOnboardingOpen(true)}
      />
      <main className="mm-main">
        <ToastBanner message={message} onDismiss={() => setMessage("")} />

        {/* Persistent owner banner removed — onboarding is now a one-time
         * modal opened automatically on first launch (or re-opened from the
         * sidebar identity chip). */}

        {extractProgress && (
          <div className="mm-extract-progress" role="status" aria-live="polite">
            <div className="mm-row" style={{ justifyContent: "space-between", fontSize: 12 }}>
              <span className="mm-lbl">
                {extractProgress.stage || "processing"} · {extractProgress.status}
              </span>
              {typeof extractProgress.progress === "number" && (
                <span className="mm-mono" style={{ fontSize: 11, color: "var(--mm-ink-3)" }}>
                  {Math.round((extractProgress.progress || 0) * 100)}%
                </span>
              )}
            </div>
            <div className="mm-extract-progress-bar">
              <i style={{ width: `${Math.round((extractProgress.progress || 0) * 100)}%` }} />
            </div>
            {extractProgress.error && (
              <div style={{ fontSize: 11, color: "var(--mm-berry)" }}>{extractProgress.error}</div>
            )}
          </div>
        )}

        {activeView === "inbox" && (
          <InboxScreen
            files={inboxFiles}
            uploading={uploading}
            uploadProgress={uploadProgress}
            inputRef={fileInputRef}
            inboxPath={health?.inbox || ""}
            templates={templates}
            fileTemplates={inboxFileTemplates}
            onFileTemplateChange={(filename, value) =>
              setInboxFileTemplates({ ...inboxFileTemplates, [filename]: value })
            }
            onUpload={async (file) => queueUpload(file)}
            onRefresh={refreshInbox}
            onIngest={ingest}
            onDelete={deleteInboxFile}
            setupStatus={setupStatus}
            onJumpToSettings={() => setActiveView("settings")}
            onJumpToPeople={() => {
              setActiveView("people");
              setOnboardingOpen(true);
            }}
          />
        )}

        {activeView === "workstreams" && (
          <WorkstreamsScreen
            workstreams={workstreamIntel}
            crossMeetingQuery={crossMeetingQuery}
            setCrossMeetingQuery={setCrossMeetingQuery}
            searchResults={searchResults}
            onSearch={runCrossMeetingSearch}
            onOpenResult={openSearchResult}
            onSelectMeeting={(meetingId) => {
              setSelectedMeetingId(meetingId);
              setActiveView("review");
              setTranscriptMode(false);
            }}
            onDelete={deleteWorkstream}
            onRename={renameWorkstream}
          />
        )}

        {activeView === "people" && (
          <PeopleScreen
            people={people}
            selectedPersonId={selectedPersonId}
            personDetail={personDetail}
            onSelectPerson={(id) => void loadPersonDetail(id)}
            onClearPerson={() => {
              setSelectedPersonId(null);
              setPersonDetail(null);
            }}
            onOpenMeeting={(meetingId) => {
              setSelectedMeetingId(meetingId);
              setActiveView("review");
              setTranscriptMode(false);
              setReviewTab("summary");
            }}
            owner={owner}
            onSetOwner={saveOwner}
            onClearOwner={clearOwnerIdentity}
            onDeletePerson={(person) =>
              setConfirmPrompt({
                title: `Remove ${person.display_name} from People?`,
                body: (
                  <>
                    <p style={{ margin: 0 }}>
                      Their name will be cleared from {person.meetings.length} meeting
                      {person.meetings.length === 1 ? "" : "s"} and {person.actions.length} action
                      {person.actions.length === 1 ? "" : "s"}.
                    </p>
                    <p style={{ marginTop: 8, fontSize: 12, opacity: 0.75 }}>
                      Transcript text, recordings, and the meetings themselves are
                      untouched. You can re-name the speaker later from the
                      transcript view to bring this person back.
                    </p>
                  </>
                ),
                confirmLabel: "✕ Remove person",
                confirmTone: "danger",
                onConfirm: async () => {
                  setConfirmPrompt(null);
                  try {
                    await api.delete(`/api/people/${person.id}`);
                    setMessage(`Removed ${person.display_name} from People.`);
                    setSelectedPersonId(null);
                    setPersonDetail(null);
                    await refreshPeople();
                  } catch (error) {
                    setMessage(error instanceof Error ? error.message : "Person delete failed.");
                  }
                },
              })
            }
            onRenamePerson={async (person, newName) => {
              // v0.2.10: rename a person from the People-detail page,
              // cascading the new label to every meeting that references
              // this person_id. If the target name already exists as a
              // separate Person row, the backend merges; we surface the
              // outcome to the user.
              const clean = newName.trim();
              if (!clean || clean === person.display_name) return;
              try {
                const result = await api.post<{ result: string; person_id: number; to: string }>(
                  `/api/people/${person.id}/rename?new_name=${encodeURIComponent(clean)}`,
                );
                setMessage(
                  result.result === "merged"
                    ? `Merged ${person.display_name} into ${result.to}.`
                    : `Renamed to ${result.to}.`,
                );
                await refreshPeople();
                await loadPersonDetail(result.person_id);
              } catch (error) {
                setMessage(error instanceof Error ? error.message : "Rename failed.");
              }
            }}
            onPruneOrphans={() =>
              setConfirmPrompt({
                title: "Tidy ghost entries?",
                body: (
                  <p style={{ margin: 0 }}>
                    Removes everyone with no confirmed meetings and no open actions.
                    Safe to re-run.
                  </p>
                ),
                confirmLabel: "✕ Tidy ghosts",
                confirmTone: "danger",
                onConfirm: async () => {
                  setConfirmPrompt(null);
                  try {
                    const data = await api.post<{ removed: string[] }>(
                      `/api/people/prune-orphans`,
                    );
                    setMessage(
                      data.removed.length
                        ? `Removed ${data.removed.length} ghost entr${data.removed.length === 1 ? "y" : "ies"}.`
                        : "No ghost entries to remove.",
                    );
                    await refreshPeople();
                  } catch (error) {
                    setMessage(error instanceof Error ? error.message : "Tidy failed.");
                  }
                },
              })
            }
          />
        )}

        {activeView === "archive" && (
          <ArchiveScreen
            timeline={timeline}
            onOpenMeeting={(meetingId) => {
              setSelectedMeetingId(meetingId);
              setActiveView("review");
              setTranscriptMode(false);
              setReviewTab("summary");
            }}
            onRefresh={refreshTimeline}
          />
        )}

        {activeView === "settings" && (
          <SettingsScreen
            health={health}
            settings={settings}
            draft={settingsDraft}
            setDraft={setSettingsDraft}
            templates={templates}
            maintenanceEnabled={maintenanceEnabled}
            maintenanceStatus={maintenanceStatus}
            maintenanceTime={maintenanceTime}
            setMaintenanceTime={setMaintenanceTime}
            onSaveSettings={saveSettings}
            onConfigureMaintenance={configureDailyMaintenance}
            onRefreshSettings={refreshSettings}
            onBackendBouncing={setBackendBouncing}
            settingsErrors={settingsErrors}
          />
        )}

        {/* v0.2.11: repair-proposal banners INTENTIONALLY hidden from
            the meeting view. The user is here to read the meeting —
            transcript, minutes, summary — not to triage system
            internals. The system either auto-applies what it's
            confident about (silent, on persist) or leaves the
            proposal in `status='open'` as an audit record. A future
            ops surface can surface those rows for power users; the
            meeting view stays read-only-of-content. */}
        {activeView === "review" && (
          <ReviewScreen
            meetings={meetings}
            detail={detail}
            selectedMeetingId={selectedMeetingId}
            onSelectMeeting={(meetingId) => {
              setSelectedMeetingId(meetingId);
              setTranscriptMode(false);
              setReviewTab("summary");
            }}
            reviewTab={reviewTab}
            setReviewTab={setReviewTab}
            transcriptMode={transcriptMode}
            setTranscriptMode={setTranscriptMode}
            summary={summaryText}
            readyForPromotion={readyForPromotion}
            obsidianAvailable={health?.obsidian_available ?? true}
            onSaveSummary={saveSummary}
            onPromote={runPromote}
            onExportPdf={exportPdf}
            onExportHtml={exportHtml}
            query={query}
            setQuery={setQuery}
            filteredSegments={filteredSegments}
            segmentById={segmentById}
            candidates={detail?.candidates || []}
            keyTerms={keyTerms}
            ownerTerms={ownerTerms}
            showConfidenceChips={showConfidenceChips}
            speakerNumberOf={speakerNumberOf}
            speakerDisplayNameById={speakerDisplayNameById}
            speakerAliasById={speakerAliasById}
            chapters={chapters}
            activeSegmentId={activeSegmentId}
            setActiveSegmentId={setActiveSegmentId}
            onCorrectSegment={correctSegment}
            onEditSpeaker={setSpeakerEditSegment}
            onRunAudioAlternatives={runAudioAlternatives}
            onAcceptAlternative={acceptAudioAlternative}
            onAfterSegmentRevert={refreshAfterSegmentRevert}
            onDeleteMeeting={async (id, title) => requestDeleteMeeting(id, title)}
            templates={templates}
            onSetTemplate={setMeetingTemplate}
            onRenameMeeting={async (id, title) => {
              await api.patch<{ status: string }>(`/api/meetings/${id}`, { title });
              setDetail(await api.get<MeetingDetail>(`/api/meetings/${id}`));
              await refreshMeetings();
            }}
          />
        )}

        <button
          type="button"
          className="mm-mobile-palette-fab"
          onClick={() => setPaletteOpen(true)}
          aria-label="Open command palette"
          title="Search meetings, people, and workstreams"
        >
          ⌘
        </button>

        {paletteOpen && (
          <CommandPalette
            meetings={meetings}
            people={people}
            workstreams={workstreamIntel}
            owner={owner}
            recentIds={recentPaletteIds}
            onClose={() => setPaletteOpen(false)}
            onAction={(action) => {
              setPaletteOpen(false);
              pushRecentPaletteAction(paletteActionKey(action));
              if (action.kind === "meeting") {
                setSelectedMeetingId(action.id);
                setActiveView("review");
                setTranscriptMode(false);
                setReviewTab("summary");
              } else if (action.kind === "person") {
                setActiveView("people");
                void loadPersonDetail(action.id);
              } else if (action.kind === "workstream") {
                setActiveView("workstreams");
                setCrossMeetingQuery(action.name);
              } else if (action.kind === "view") {
                setActiveView(action.target);
                if (action.target === "people") void refreshPeople();
                if (action.target === "archive") void refreshTimeline();
              } else if (action.kind === "me-actions" && owner.person_id) {
                setActiveView("people");
                void loadPersonDetail(owner.person_id);
              }
            }}
          />
        )}

        {speakerEditSegment && (
          <SpeakerEditModal
            segment={speakerEditSegment}
            speakerIds={speakerIds}
            speakerAliasById={speakerAliasById}
            speakerDisplayNameById={speakerDisplayNameById}
            speakerNumberOf={speakerNumberOf}
            suggestions={speakerSuggestions.filter(
              (suggestion) => suggestion.speakerId === speakerEditSegment.diarization_speaker_id,
            )}
            onRename={renameSpeaker}
            onMoveCard={(targetSpeakerId) => moveSegmentSpeaker(speakerEditSegment, targetSpeakerId)}
            onMoveAll={(targetSpeakerId) =>
              moveAllSpeakerSegments(speakerEditSegment.diarization_speaker_id, targetSpeakerId)
            }
            onClose={() => setSpeakerEditSegment(null)}
          />
        )}

        {postIngestSpeakerMeeting !== null && detail && (
          <PostIngestSpeakerModal
            speakerIds={speakerIds}
            speakerDisplayNameById={speakerDisplayNameById}
            speakerNumberOf={speakerNumberOf}
            bestSpeakerSuggestionById={bestSpeakerSuggestionById}
            allSuggestions={speakerSuggestions}
            savedSpeakerIds={savedSpeakerIds}
            onConfirm={async (assignments) => {
              for (const [speakerId, label] of assignments) {
                await approveSpeakerName(
                  speakerId,
                  label,
                  `Confirmed ${label}.`,
                );
              }
            }}
            onSkip={() => {
              if (detail) markPostIngestSeen(detail.meeting.id);
              setPostIngestSpeakerMeeting(null);
            }}
            onOpenTranscript={() => {
              if (detail) markPostIngestSeen(detail.meeting.id);
              setPostIngestSpeakerMeeting(null);
              setTranscriptMode(true);
              setReviewTab("transcript");
            }}
          />
        )}

        {deletePrompt && (
          <ConfirmModal
            title="Delete this meeting?"
            body={
              <>
                <p style={{ margin: 0 }}>
                  This permanently removes <strong>{deletePrompt.title}</strong>,
                  its transcript, and the source recording.
                </p>
                <p style={{ marginTop: 8, fontSize: 12, opacity: 0.75 }}>
                  Any vault note sent to Obsidian will be removed too. This
                  cannot be undone.
                </p>
              </>
            }
            confirmLabel="✕ Delete meeting"
            confirmTone="danger"
            onConfirm={() => void confirmDeleteMeeting()}
            onCancel={() => setDeletePrompt(null)}
          />
        )}

        {confirmPrompt && (
          <ConfirmModal
            title={confirmPrompt.title}
            body={confirmPrompt.body}
            confirmLabel={confirmPrompt.confirmLabel}
            confirmTone={confirmPrompt.confirmTone}
            onConfirm={confirmPrompt.onConfirm}
            onCancel={() => setConfirmPrompt(null)}
          />
        )}

        {pendingUpload && (
          <IngestTemplateModal
            filename={pendingUpload.name}
            templates={templates}
            onConfirm={(template) => void runQueuedUpload(template)}
            onCancel={() => setPendingUpload(null)}
          />
        )}

        {onboardingOpen && (
          <OnboardingModal
            owner={owner}
            suggestion={ownerSuggestion}
            people={people}
            onSave={(personId, displayName, aliases) => {
              localStorage.setItem("mm-onboarding-dismissed", "1");
              setOnboardingOpen(false);
              void saveOwner(personId, displayName, aliases);
            }}
            onSkip={() => {
              localStorage.setItem("mm-onboarding-dismissed", "1");
              setOnboardingOpen(false);
            }}
            onLoadPeople={async () => {
              await refreshPeople();
            }}
          />
        )}

        {versionInfo && shouldShowUpgrade(versionInfo) && !upgradeModalOpen && (
          <UpgradePill
            info={versionInfo}
            onClick={() => setUpgradeModalOpen(true)}
          />
        )}

        {upgradeModalOpen && versionInfo && (
          <UpgradeModal
            info={versionInfo}
            upgrading={upgrading}
            onClose={() => setUpgradeModalOpen(false)}
            onSnooze={() => {
              if (versionInfo.latest) snoozeUpgrade(versionInfo.latest);
              setUpgradeModalOpen(false);
            }}
            onUpgrade={async () => {
              setUpgrading(true);
              setBackendBouncing(true);
              try {
                await api.post("/api/system/upgrade");
                // Poll for the backend to come back. /api/health is the
                // cheapest endpoint that proves the new code is live.
                const deadline = Date.now() + 180_000; // 3 minutes
                while (Date.now() < deadline) {
                  await new Promise((r) => setTimeout(r, 2000));
                  try {
                    await api.get("/api/health");
                    // Backend is back — force a fresh JS bundle too.
                    window.location.reload();
                    return;
                  } catch {
                    // still down, keep polling
                  }
                }
                setMessage(
                  "Upgrade started but the backend hasn't come back yet. Run `mm status` from a terminal.",
                );
              } catch (error) {
                setMessage(
                  error instanceof Error
                    ? `Upgrade failed: ${error.message}`
                    : "Upgrade failed.",
                );
              } finally {
                setUpgrading(false);
                setBackendBouncing(false);
              }
            }}
          />
        )}
      </main>
    </div>
    </SpeakerNameProvider>
  );
}

// ── Upgrade pill + modal ───────────────────────────────────────────────────
// localStorage shape: { until: ISOString, version: string }. The pill is
// hidden until `until` has passed AND the latest version differs from the
// snoozed version. So a fresh release always shows even if a previous one
// was snoozed.
const UPGRADE_SNOOZE_KEY = "mm-upgrade-snooze";
const UPGRADE_SNOOZE_MS = 24 * 60 * 60 * 1000; // 24h

function shouldShowUpgrade(info: VersionInfo | null): boolean {
  if (!info?.upgrade_available || !info.latest) return false;
  try {
    const raw = localStorage.getItem(UPGRADE_SNOOZE_KEY);
    if (!raw) return true;
    const parsed = JSON.parse(raw) as { until?: string; version?: string };
    if (parsed.version !== info.latest) return true;
    const until = parsed.until ? Date.parse(parsed.until) : 0;
    return Date.now() >= until;
  } catch {
    return true;
  }
}

function snoozeUpgrade(version: string) {
  try {
    localStorage.setItem(
      UPGRADE_SNOOZE_KEY,
      JSON.stringify({
        until: new Date(Date.now() + UPGRADE_SNOOZE_MS).toISOString(),
        version,
      }),
    );
  } catch {
    // localStorage disabled or full — silently skip; pill will show again next load.
  }
}

function UpgradePill({
  info,
  onClick,
}: {
  info: VersionInfo;
  onClick: () => void;
}) {
  return (
    <button type="button" className="mm-upgrade-pill" onClick={onClick}>
      <span className="mm-upgrade-pill-dot" aria-hidden="true" />
      <span>
        <strong>{info.latest}</strong> available
      </span>
      <span className="mm-upgrade-pill-arrow" aria-hidden="true">→</span>
    </button>
  );
}

function UpgradeModal({
  info,
  upgrading,
  onUpgrade,
  onSnooze,
  onClose,
}: {
  info: VersionInfo;
  upgrading: boolean;
  onUpgrade: () => Promise<void>;
  onSnooze: () => void;
  onClose: () => void;
}) {
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !upgrading) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose, upgrading]);

  return (
    <div
      className="mm-modal-backdrop"
      role="presentation"
      onClick={() => !upgrading && onClose()}
    >
      <section
        className="mm-modal mm-modal-upgrade"
        role="dialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mm-modal-head">
          <div className="mm-lbl-strong">Update</div>
          {!upgrading && (
            <button type="button" className="mm-btn mm-btn-ghost" onClick={onClose}>
              Close ✕
            </button>
          )}
        </header>
        <div className="mm-modal-body">
          <h2>
            MeetingMind <Ital>{info.latest}</Ital>
          </h2>
          <p className="mm-modal-sub" style={{ marginTop: 6 }}>
            You're on <strong>{info.current}</strong>.{" "}
            {info.release_url && (
              <a
                href={info.release_url}
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--mm-clay)" }}
              >
                View release on GitHub ↗
              </a>
            )}
          </p>
        </div>
        <div className="mm-modal-section">
          <div className="mm-lbl-strong" style={{ marginBottom: 8 }}>What's new</div>
          <pre className="mm-upgrade-notes">
            {info.release_notes || "(no release notes provided)"}
          </pre>
        </div>
        <div className="mm-modal-section" style={{ paddingTop: 8 }}>
          <div
            style={{
              display: "flex",
              gap: 8,
              justifyContent: "flex-end",
              flexWrap: "wrap",
            }}
          >
            <button
              type="button"
              className="mm-btn"
              onClick={onSnooze}
              disabled={upgrading}
              title="Hide this notification for 24 hours"
            >
              Remind me later
            </button>
            <button
              type="button"
              className="mm-btn mm-btn-primary"
              onClick={() => void onUpgrade()}
              disabled={upgrading}
            >
              {upgrading ? "Upgrading…" : "↻ Upgrade now"}
            </button>
          </div>
          {upgrading && (
            <p
              className="mm-modal-sub"
              style={{ marginTop: 12, fontSize: 12 }}
            >
              Pulling latest, refreshing dependencies, and restarting the
              backend. The dashboard will reload automatically when the new
              backend answers. Takes ~30-60 seconds.
            </p>
          )}
        </div>
      </section>
    </div>
  );
}


// RestartSystemButton — fires POST /api/system/restart, which spawns
// `meetingmind restart` in a detached subprocess. The backend dies, then
// the new process answers /api/health. We poll until it's back up so the
// user sees confirmation, not a dead UI.
function RestartSystemButton({
  onAfterRestart,
  onBouncing,
}: {
  onAfterRestart: () => void;
  onBouncing: (bouncing: boolean) => void;
}) {
  const [phase, setPhase] = useState<"idle" | "confirm" | "restarting" | "done">("idle");
  const [error, setError] = useState("");

  const run = async () => {
    setPhase("restarting");
    setError("");
    // Tell App-level message routing that any network failures over the
    // next ~30s are expected restart noise, not real errors.
    onBouncing(true);
    try {
      await fetch("/api/system/restart", { method: "POST" });
    } catch {
      // The backend often closes the connection mid-response when it
      // gets killed — that's expected. Continue to the polling phase.
    }
    // Poll /api/health every 600ms until the new process answers, then
    // refresh the Settings view. Cap at ~30s so a permanent failure
    // surfaces an error instead of spinning forever.
    const deadline = Date.now() + 30_000;
    let alive = false;
    while (Date.now() < deadline) {
      await new Promise((resolve) => setTimeout(resolve, 600));
      try {
        const response = await fetch("/api/health");
        if (response.ok) {
          alive = true;
          break;
        }
      } catch {
        /* still down */
      }
    }
    onBouncing(false);
    if (!alive) {
      setError("Backend didn't come back online — check `meetingmind status`.");
      setPhase("idle");
      return;
    }
    onAfterRestart();
    setPhase("done");
    window.setTimeout(() => setPhase("idle"), 2500);
  };

  if (phase === "confirm") {
    return (
      <ConfirmModal
        title="Restart system?"
        body={
          <>
            <p style={{ margin: 0 }}>
              This restarts the MeetingMind backend (and the frontend dev server,
              if running under <code>meetingmind start</code>).
            </p>
            <p style={{ marginTop: 8, fontSize: 12, opacity: 0.75 }}>
              In-progress extracts may be interrupted. Most settings apply
              without a restart — only port changes and rare model-loader
              quirks usually need one.
            </p>
          </>
        }
        confirmLabel="↻ Restart now"
        confirmTone="primary"
        onConfirm={() => void run()}
        onCancel={() => setPhase("idle")}
      />
    );
  }

  return (
    <button
      type="button"
      className={phase === "restarting" ? "mm-btn mm-btn-clay" : "mm-btn"}
      onClick={() => setPhase("confirm")}
      disabled={phase === "restarting"}
      title={
        error
          ||
          (phase === "restarting"
            ? "Waiting for the backend to come back online…"
            : "Restart backend + frontend (managed services). Most settings don't need this.")
      }
    >
      {phase === "restarting"
        ? "↻ Restarting…"
        : phase === "done"
          ? "✓ Restarted"
          : "↻ Restart system"}
    </button>
  );
}

// Post-ingest speaker modal — pops the first time a user lands on a
// freshly-ingested meeting that still has unconfirmed speakers. Pulls
// suggested names from the existing review_items (LLM speaker-name
// candidates + voice-profile matches) so the user picks from a ranked
// list instead of typing every name cold.
/** v0.2.8: surfaces v0.2.4 speaker-reattribution proposals at the top of
 * the Review view. Each proposal: "Speaker 1 → Alice at segment #12 —
 * basis. [Apply] [Dismiss]". Backend endpoints in v0.2.7 do the actual
 * transcript update + review_item resolution.
 */
type ReattributionProposal = {
  reviewItemId: number;
  segmentId: number;
  currentSpeaker: string;
  proposedSpeaker: string;
  confidence: number;
  basis: string;
};

function RepairProposalsBanner({
  proposals,
  speakerDisplayNameById,
  onAccept,
  onReject,
}: {
  proposals: ReattributionProposal[];
  speakerDisplayNameById: Map<string, string>;
  onAccept: (reviewItemId: number, proposedSpeaker: string) => void | Promise<void>;
  onReject: (reviewItemId: number) => void | Promise<void>;
}) {
  // v0.2.10 audit: 7+ proposals push the meeting hero below the fold.
  // Default collapsed so the suggestions are available but not blocking.
  const [expanded, setExpanded] = useState(false);
  return (
    <section
      className="mm-panel"
      style={{ margin: "0 0 16px 0", padding: 16 }}
      aria-label="Pending speaker-label suggestions"
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: expanded ? 8 : 0,
          gap: 12,
        }}
      >
        <button
          type="button"
          className="mm-btn mm-btn-ghost mm-btn-sm"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          style={{ padding: "2px 8px", fontSize: 12 }}
        >
          {expanded ? "▾" : "▸"}
        </button>
        <div className="mm-lbl-strong" style={{ flex: 1 }}>
          Speaker corrections suggested ({proposals.length})
        </div>
        <span className="mm-lbl" style={{ fontSize: 11 }}>
          {expanded
            ? "from conversational context — Apply to update the transcript"
            : "click to review"}
        </span>
      </header>
      {!expanded ? null : (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {proposals.slice(0, 12).map((proposal) => {
          const currentName =
            speakerDisplayNameById.get(proposal.currentSpeaker) ||
            proposal.currentSpeaker ||
            "Unknown";
          const proposedName =
            speakerDisplayNameById.get(proposal.proposedSpeaker) ||
            proposal.proposedSpeaker;
          return (
            <div
              key={proposal.reviewItemId}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "8px 10px",
                borderRadius: 8,
                background: "var(--mm-ink-0, rgba(255,255,255,0.03))",
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13 }}>
                  <span className="mm-mono">#{proposal.segmentId}</span>{" "}
                  <span className="mm-lbl">re-label</span>{" "}
                  <strong>{currentName}</strong>{" "}
                  <span className="mm-lbl">→</span>{" "}
                  <strong>{proposedName}</strong>{" "}
                  <span
                    className="mm-mono"
                    style={{ color: "var(--mm-ink-3)", fontSize: 11 }}
                  >
                    {Math.round(proposal.confidence * 100)}%
                  </span>
                </div>
                {proposal.basis && (
                  <div
                    className="mm-lbl"
                    style={{ fontSize: 11, marginTop: 2, opacity: 0.8 }}
                  >
                    {proposal.basis}
                  </div>
                )}
              </div>
              <button
                type="button"
                className="mm-btn mm-btn-sm mm-btn-primary"
                onClick={() =>
                  void onAccept(proposal.reviewItemId, proposal.proposedSpeaker)
                }
              >
                Apply
              </button>
              <button
                type="button"
                className="mm-btn mm-btn-sm mm-btn-ghost"
                onClick={() => void onReject(proposal.reviewItemId)}
              >
                Dismiss
              </button>
            </div>
          );
        })}
        {proposals.length > 12 && (
          <div className="mm-lbl" style={{ fontSize: 11, opacity: 0.7 }}>
            … and {proposals.length - 12} more — applying or dismissing the
            first batch lets the rest surface.
          </div>
        )}
      </div>
      )}
    </section>
  );
}

/**
 * v0.2.10 Pass D: segment-split proposals banner. Surfaces low-confidence
 * boundary-leak detections from `repair.segment_splitter`. Each proposal:
 * "Split seg #15 — Speaker 2 keeps the head, Speaker 4 gets the tail.
 *  Tail: 'Okay, so I am of'  [Apply] [Dismiss]"
 *
 * Same lifecycle as the reattribution banner: accepting hits
 * `/accept-split` which shrinks the head segment, inserts a new tail
 * with the proposed speaker, and re-points word-level timestamps.
 */
type SplitProposal = {
  reviewItemId: number;
  segmentId: number;
  splitAtMs: number;
  headText: string;
  tailText: string;
  tailSpeakerId: string;
  evidence: string;
  confidence: number;
};

function SplitProposalsBanner({
  proposals,
  speakerDisplayNameById,
  onAccept,
  onReject,
}: {
  proposals: SplitProposal[];
  speakerDisplayNameById: Map<string, string>;
  onAccept: (reviewItemId: number) => void | Promise<void>;
  onReject: (reviewItemId: number) => void | Promise<void>;
}) {
  // v0.2.10 audit: collapse by default so 7+ proposals don't push the
  // meeting header below the fold. User clicks the chevron to triage.
  const [expanded, setExpanded] = useState(false);
  return (
    <section
      className="mm-panel"
      style={{ margin: "0 0 16px 0", padding: 16 }}
      aria-label="Pending segment-split suggestions"
    >
      <header
        style={{
          display: "flex",
          alignItems: "center",
          justifyContent: "space-between",
          marginBottom: expanded ? 8 : 0,
          gap: 12,
        }}
      >
        <button
          type="button"
          className="mm-btn mm-btn-ghost mm-btn-sm"
          onClick={() => setExpanded((v) => !v)}
          aria-expanded={expanded}
          style={{ padding: "2px 8px", fontSize: 12 }}
        >
          {expanded ? "▾" : "▸"}
        </button>
        <div className="mm-lbl-strong" style={{ flex: 1 }}>
          Segment splits suggested ({proposals.length})
        </div>
        <span className="mm-lbl" style={{ fontSize: 11 }}>
          {expanded
            ? "boundary leaks where the next speaker's words got stitched on"
            : "click to review"}
        </span>
      </header>
      {!expanded ? null : (
      <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
        {proposals.slice(0, 8).map((proposal) => {
          const tailName =
            speakerDisplayNameById.get(proposal.tailSpeakerId) ||
            proposal.tailSpeakerId;
          const minutes = Math.floor(proposal.splitAtMs / 60_000);
          const seconds = Math.floor((proposal.splitAtMs % 60_000) / 1000);
          const tsLabel = `${minutes.toString().padStart(2, "0")}:${seconds
            .toString()
            .padStart(2, "0")}`;
          return (
            <div
              key={proposal.reviewItemId}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 12,
                padding: "8px 10px",
                borderRadius: 8,
                background: "var(--mm-ink-0, rgba(255,255,255,0.03))",
              }}
            >
              <div style={{ flex: 1, minWidth: 0 }}>
                <div style={{ fontSize: 13 }}>
                  <span className="mm-mono">#{proposal.segmentId}</span>{" "}
                  <span className="mm-lbl">split at</span>{" "}
                  <span className="mm-mono">{tsLabel}</span>{" "}
                  <span className="mm-lbl">→ tail to</span>{" "}
                  <strong>{tailName}</strong>{" "}
                  <span
                    className="mm-mono"
                    style={{ color: "var(--mm-ink-3)", fontSize: 11 }}
                  >
                    {Math.round(proposal.confidence * 100)}%
                  </span>
                </div>
                <div
                  className="mm-lbl"
                  style={{
                    fontSize: 11,
                    marginTop: 2,
                    opacity: 0.8,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                  title={`${proposal.evidence} · tail: "${proposal.tailText}"`}
                >
                  tail: “{proposal.tailText}”
                </div>
              </div>
              <button
                type="button"
                className="mm-btn mm-btn-sm mm-btn-primary"
                onClick={() => void onAccept(proposal.reviewItemId)}
              >
                Apply
              </button>
              <button
                type="button"
                className="mm-btn mm-btn-sm mm-btn-ghost"
                onClick={() => void onReject(proposal.reviewItemId)}
              >
                Dismiss
              </button>
            </div>
          );
        })}
        {proposals.length > 8 && (
          <div className="mm-lbl" style={{ fontSize: 11, opacity: 0.7 }}>
            … and {proposals.length - 8} more — applying or dismissing the
            first batch lets the rest surface.
          </div>
        )}
      </div>
      )}
    </section>
  );
}

function PostIngestSpeakerModal({
  speakerIds,
  speakerDisplayNameById,
  speakerNumberOf,
  bestSpeakerSuggestionById,
  allSuggestions,
  savedSpeakerIds,
  onConfirm,
  onSkip,
  onOpenTranscript,
}: {
  speakerIds: string[];
  speakerDisplayNameById: Map<string, string>;
  speakerNumberOf: (id: string) => number;
  bestSpeakerSuggestionById: Map<string, SpeakerSuggestion>;
  allSuggestions: SpeakerSuggestion[];
  savedSpeakerIds: Set<string>;
  onConfirm: (assignments: Array<[string, string]>) => Promise<void>;
  onSkip: () => void;
  onOpenTranscript: () => void;
}) {
  // One draft per unconfirmed speaker. Pre-fill with the best suggestion
  // when available so the common case is one click.
  const unconfirmed = speakerIds.filter((id) => !savedSpeakerIds.has(id));
  // Drafts start empty so the user actively picks a name — either by
  // clicking a suggestion pill above the input or typing. Pre-filling
  // with the top suggestion made every speaker feel like an
  // AI-assigned name the user had to correct; this flips it so the
  // AI is an assistant, not an opinion.
  const [drafts, setDrafts] = useState<Record<string, string>>(() => {
    const initial: Record<string, string> = {};
    unconfirmed.forEach((id) => {
      initial[id] = "";
    });
    return initial;
  });
  const [saving, setSaving] = useState(false);

  const handleConfirm = async () => {
    const assignments: Array<[string, string]> = unconfirmed
      .map((id): [string, string] => [id, drafts[id]?.trim() ?? ""])
      .filter(([, name]) => !!name);
    if (!assignments.length) {
      onSkip();
      return;
    }
    setSaving(true);
    try {
      await onConfirm(assignments);
      onSkip();
    } finally {
      setSaving(false);
    }
  };

  const hasPartialDrafts = Object.values(drafts).some((value) => value.trim().length > 0);
  const handleSpeakerBackdrop = () => {
    // Same as Onboarding — don't toss away half-typed speaker names on a
    // stray click outside the sheet.
    if (hasPartialDrafts) return;
    onSkip();
  };

  return (
    <div className="mm-modal-backdrop" role="presentation" onClick={handleSpeakerBackdrop}>
      <section
        className="mm-modal mm-modal-speakers"
        role="dialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mm-modal-head">
          <div className="mm-lbl-strong">New meeting · {unconfirmed.length} unconfirmed</div>
          <button type="button" className="mm-btn mm-btn-ghost" onClick={onSkip}>
            Skip ✕
          </button>
        </header>
        <div className="mm-modal-body">
          <h2>
            Confirm <Ital>who's who</Ital>
          </h2>
          <div className="mm-modal-sub">
            MeetingMind matched each voice to a likely name. Tap a suggestion,
            type a name, or skip — you can always edit later from the
            transcript.
          </div>
        </div>

        <div className="mm-modal-section mm-post-ingest-list">
          {unconfirmed.map((speakerId) => {
            const number = speakerNumberOf(speakerId);
            const displayName = speakerDisplayNameById.get(speakerId) || speakerId;
            const suggestions = allSuggestions.filter(
              (s) => s.speakerId === speakerId,
            );
            const value = drafts[speakerId] ?? "";
            return (
              <div key={speakerId} className="mm-post-ingest-row">
                <div className="mm-post-ingest-head">
                  <SpeakerAvatar
                    name={displayName}
                    speakerNumber={number}
                    size={32}
                  />
                  <span className="mm-mono" style={{ fontSize: 11, opacity: 0.7 }}>
                    Speaker {number}
                  </span>
                </div>
                {suggestions.length > 0 && (
                  <div className="mm-post-ingest-suggestions">
                    {suggestions.slice(0, 5).map((suggestion, index) => (
                      <button
                        key={`${suggestion.candidateName}-${index}`}
                        type="button"
                        className={
                          value === suggestion.candidateName
                            ? "mm-post-ingest-suggestion is-picked"
                            : "mm-post-ingest-suggestion"
                        }
                        onClick={() =>
                          setDrafts({ ...drafts, [speakerId]: suggestion.candidateName })
                        }
                      >
                        <span>{suggestion.candidateName}</span>
                        {typeof suggestion.confidence === "number" && (
                          <small className="mm-mono">
                            {Math.round(suggestion.confidence * 100)}%
                          </small>
                        )}
                      </button>
                    ))}
                  </div>
                )}
                <input
                  className="mm-input mm-input-square"
                  placeholder="Type a name or pick a suggestion above"
                  value={value}
                  onChange={(event) =>
                    setDrafts({ ...drafts, [speakerId]: event.target.value })
                  }
                  autoComplete="off"
                />
              </div>
            );
          })}
        </div>

        <div className="mm-modal-section" style={{ paddingTop: 8 }}>
          <div
            style={{
              display: "flex",
              gap: 8,
              justifyContent: "space-between",
              alignItems: "center",
              flexWrap: "wrap",
            }}
          >
            <button
              type="button"
              className="mm-btn mm-btn-ghost"
              onClick={onOpenTranscript}
              disabled={saving}
            >
              Open transcript instead
            </button>
            <div style={{ display: "flex", gap: 8 }}>
              <button
                type="button"
                className="mm-btn"
                onClick={onSkip}
                disabled={saving}
              >
                Skip for now
              </button>
              <button
                type="button"
                className="mm-btn mm-btn-primary"
                onClick={() => void handleConfirm()}
                disabled={saving}
              >
                {saving ? "Saving…" : "✓ Confirm"}
              </button>
            </div>
          </div>
        </div>
      </section>
    </div>
  );
}

// ── Ingest template modal — pops when the user drops a file via the
// drag-drop or "Choose a file" button. Forces a per-meeting type
// selection before the upload + transcription pass fires, since the
// extraction prompt depends on the choice.
function IngestTemplateModal({
  filename,
  templates,
  onConfirm,
  onCancel,
}: {
  filename: string;
  templates: TemplateOption[];
  onConfirm: (template: string) => void;
  onCancel: () => void;
}) {
  const [selected, setSelected] = useState<string>("");
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onCancel();
      if (event.key === "Enter" && selected) onConfirm(selected);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [selected, onConfirm, onCancel]);

  return (
    <div className="mm-modal-backdrop" role="presentation" onClick={onCancel}>
      <section
        className="mm-modal mm-modal-ingest"
        role="dialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mm-modal-head">
          <div className="mm-lbl-strong">New recording</div>
          <button type="button" className="mm-btn mm-btn-ghost" onClick={onCancel}>
            Cancel ✕
          </button>
        </header>
        <div className="mm-modal-body">
          <h2>
            What kind of <Ital>meeting</Ital> is this?
          </h2>
          <div className="mm-modal-sub">
            <code>{filename}</code>
          </div>
        </div>
        <div className="mm-modal-section">
          <p className="mm-modal-sub" style={{ marginBottom: 12 }}>
            The meeting type tunes how MeetingMind extracts actions,
            decisions, and topics. No default — pick the closest match.
          </p>
          <div className="mm-ingest-template-grid">
            {templates.map((option) => (
              <button
                key={option.id}
                type="button"
                className={
                  selected === option.id
                    ? "mm-ingest-template is-picked"
                    : "mm-ingest-template"
                }
                onClick={() => setSelected(option.id)}
              >
                <span className="mm-ingest-template-name">{option.name}</span>
                <span className="mm-ingest-template-id mm-mono">{option.id}</span>
              </button>
            ))}
          </div>
          <div
            style={{
              display: "flex",
              justifyContent: "flex-end",
              gap: 8,
              marginTop: 16,
            }}
          >
            <button type="button" className="mm-btn" onClick={onCancel}>
              Cancel
            </button>
            <button
              type="button"
              className="mm-btn mm-btn-primary"
              disabled={!selected}
              onClick={() => onConfirm(selected)}
            >
              ✓ Start transcribing
            </button>
          </div>
          <small style={{ display: "block", marginTop: 8, opacity: 0.6 }}>
            Enter to confirm · Esc to cancel
          </small>
        </div>
      </section>
    </div>
  );
}

// ── EditableTitle — click the rendered title to edit in place, save on
// blur or Enter, cancel on Escape. Used for the meeting hero title so
// users can rename a meeting without leaving the page.
function EditableTitle({
  value,
  onSave,
  render,
}: {
  value: string;
  onSave: (next: string) => Promise<void>;
  render: (text: string) => React.ReactNode;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [value, editing]);
  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);
  const commit = async () => {
    const next = draft.trim();
    if (!next || next === value) {
      setEditing(false);
      setDraft(value);
      return;
    }
    setSaving(true);
    try {
      await onSave(next);
      setEditing(false);
    } catch {
      setEditing(false);
      setDraft(value);
    } finally {
      setSaving(false);
    }
  };
  if (editing) {
    return (
      <input
        ref={inputRef}
        className="mm-editable-title-input"
        value={draft}
        disabled={saving}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={() => void commit()}
        onKeyDown={(event) => {
          if (event.key === "Enter") void commit();
          if (event.key === "Escape") {
            setEditing(false);
            setDraft(value);
          }
        }}
      />
    );
  }
  return (
    <span
      className="mm-editable-title"
      role="button"
      tabIndex={0}
      title="Click to rename"
      onClick={() => setEditing(true)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          setEditing(true);
        }
      }}
    >
      {render(value)}
    </span>
  );
}

// ── Toast banner: auto-dismissing replacement for the old persistent
// "mm-notice" pill at the top of the main canvas. Floats top-right,
// fades out after a few seconds. Errors live longer than success messages.
function ToastBanner({
  message,
  onDismiss,
}: {
  message: string;
  onDismiss: () => void;
}) {
  useEffect(() => {
    if (!message) return undefined;
    const looksLikeError = /fail|error|blocked|cannot|denied/i.test(message);
    const timeout = looksLikeError ? 10_000 : 4_000;
    const handle = window.setTimeout(onDismiss, timeout);
    return () => window.clearTimeout(handle);
  }, [message, onDismiss]);

  if (!message) return null;
  const looksLikeError = /fail|error|blocked|cannot|denied/i.test(message);
  return (
    <div className="mm-toast-stack" role="status" aria-live="polite">
      <div className={looksLikeError ? "mm-toast mm-toast-error" : "mm-toast"}>
        <span>{message}</span>
        <button
          type="button"
          className="mm-toast-dismiss"
          onClick={onDismiss}
          aria-label="Dismiss"
        >
          ✕
        </button>
      </div>
    </div>
  );
}

// ── Onboarding modal: first-launch identity flow ────────────────────────
// Replaces the persistent "Is this you?" banner with a one-time modal that
// asks for the user's display name, confirms a likely speaker match from
// existing meetings, and lets them skip if they're not ready.
function OnboardingModal({
  owner,
  suggestion,
  people,
  onSave,
  onSkip,
  onLoadPeople,
}: {
  owner: OwnerIdentity;
  suggestion: OwnerSuggestion | null;
  people: PersonSummary[];
  onSave: (personId: number | null, displayName: string | null, aliases: string[]) => void;
  onSkip: () => void;
  onLoadPeople: () => Promise<void>;
}) {
  const [step, setStep] = useState<"name" | "match">("name");
  const [displayName, setDisplayName] = useState(
    owner.display_name || suggestion?.display_name || "",
  );
  const [selectedPersonId, setSelectedPersonId] = useState<number | null>(
    suggestion?.person_id ?? null,
  );

  useEffect(() => {
    void onLoadPeople();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onSkip();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onLoadPeople, onSkip]);

  const handleConfirm = () => {
    if (!displayName.trim()) return;
    onSave(selectedPersonId, displayName.trim(), []);
  };

  const handleBackdropClick = () => {
    // Always dismiss on backdrop click. The previous "keep open if a
    // name was typed" guard meant a first-time tester who clicked into
    // the dashboard once and saw the modal couldn't get past it —
    // every subsequent click hit the backdrop and looked like the app
    // was frozen. Onboarding is opt-in; if they don't want it, get
    // out of their way. The dismiss flag is set in the parent's
    // onSkip so the next page load is clean.
    onSkip();
  };

  return (
    <div className="mm-modal-backdrop" role="presentation" onClick={handleBackdropClick}>
      <section
        className="mm-modal mm-modal-onboarding"
        role="dialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mm-modal-head">
          <div className="mm-lbl-strong">Welcome</div>
          <button type="button" className="mm-btn mm-btn-ghost" onClick={onSkip}>
            Set later
          </button>
        </header>
        <div className="mm-modal-body">
          <h2>
            Tell MeetingMind <Ital>who you are</Ital>
          </h2>
          <div className="mm-modal-sub">
            Two short steps. Your actions, mentions, and workstreams will surface first across every meeting.
          </div>
        </div>

        {step === "name" && (
          <div className="mm-modal-section">
            <h3>What should we call you?</h3>
            <input
              autoFocus
              className="mm-input mm-input-square"
              value={displayName}
              placeholder="e.g. Wolfgang Amadeus Mozart"
              onChange={(event) => setDisplayName(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && displayName.trim()) setStep("match");
              }}
              style={{ width: "100%", marginTop: 8 }}
            />
            <small style={{ display: "block", marginTop: 8, opacity: 0.7 }}>
              We use this name to match your voice + mentions across meetings.
              Nothing leaves your machine.
            </small>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                marginTop: 18,
                alignItems: "center",
              }}
            >
              <small style={{ color: "var(--mm-ink-2)", fontWeight: 600 }}>Step 1 of 2</small>
              <button
                type="button"
                className="mm-btn mm-btn-primary"
                disabled={!displayName.trim()}
                onClick={() => setStep("match")}
              >
                Next →
              </button>
            </div>
          </div>
        )}

        {step === "match" && (
          <div className="mm-modal-section">
            {people.length === 0 ? (
              <>
                <h3>You're all set</h3>
                <p className="mm-modal-sub" style={{ marginTop: 6 }}>
                  No past meetings yet — once you ingest one and confirm yourself
                  as a speaker, MeetingMind will match your voice across future
                  meetings automatically. Hit <strong>Set my identity</strong> to
                  finish; we'll wait until you record something.
                </p>
              </>
            ) : (
              <>
                <h3>Pick yourself from your existing meetings</h3>
                <p className="mm-modal-sub" style={{ marginTop: 6 }}>
                  Optional — skip this if you weren't in any of these. You can
                  always assign yourself later from any meeting's speaker controls.
                </p>
              </>
            )}
            {suggestion && (
              <div className="mm-onboard-suggestion">
                <strong>Most-active speaker:</strong>{" "}
                {suggestion.display_name} ·{" "}
                {suggestion.meeting_count} meeting
                {suggestion.meeting_count === 1 ? "" : "s"}
                <button
                  type="button"
                  className="mm-btn mm-btn-primary mm-btn-sm"
                  style={{ marginLeft: 10 }}
                  onClick={() => setSelectedPersonId(suggestion.person_id)}
                >
                  use this
                </button>
              </div>
            )}
            <div className="mm-onboard-people">
              {people.length === 0 && (
                <p className="mm-empty">
                  No people in the system yet — process a meeting first, then come back here.
                </p>
              )}
              {people.map((person) => {
                const picked = selectedPersonId === person.id;
                return (
                  <button
                    type="button"
                    key={person.id}
                    className={
                      picked
                        ? "mm-onboard-person is-picked"
                        : "mm-onboard-person"
                    }
                    onClick={() => setSelectedPersonId(picked ? null : person.id)}
                  >
                    <SpeakerAvatar
                      name={person.display_name}
                      speakerNumber={(person.id % 6) + 1}
                      size={32}
                    />
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div className="mm-display" style={{ fontSize: 14 }}>
                        {person.display_name}
                      </div>
                      <small style={{ opacity: 0.6 }}>
                        {person.meeting_count}m · {person.action_count}{" "}
                        {person.action_count === 1 ? "action" : "actions"}
                      </small>
                    </div>
                    {picked && <span className="mm-pill-yours">⌖</span>}
                  </button>
                );
              })}
            </div>
            <div
              style={{
                display: "flex",
                justifyContent: "space-between",
                marginTop: 18,
                alignItems: "center",
              }}
            >
              <button
                type="button"
                className="mm-btn"
                onClick={() => setStep("name")}
              >
                ← Back
              </button>
              <div style={{ display: "flex", gap: 8 }}>
                <small style={{ opacity: 0.55, alignSelf: "center" }}>
                  Step 2 of 2
                </small>
                <button
                  type="button"
                  className="mm-btn mm-btn-primary"
                  onClick={handleConfirm}
                  disabled={!displayName.trim()}
                >
                  ✓ Set my identity
                </button>
              </div>
            </div>
          </div>
        )}
      </section>
    </div>
  );
}

// ── Inbox screen ──────────────────────────────────────────────────────────
function SetupChecklist({
  status,
  onJumpToSettings,
  onJumpToPeople,
}: {
  status: SetupStatus | null;
  onJumpToSettings: () => void;
  onJumpToPeople: () => void;
}) {
  if (!status || status.ready) return null;
  return (
    <div className="mm-setup-checklist">
      <div className="mm-lbl-strong">{status.blocker_count} setup task(s) before you can ingest</div>
      <ul>
        {status.items.map((item) => (
          <li key={item.id} className={item.ok ? "is-ok" : "is-pending"}>
            <span className="mm-setup-mark" aria-hidden="true">{item.ok ? "✓" : "○"}</span>
            <div style={{ flex: 1 }}>
              <strong>{item.label}</strong>
              <span style={{ color: "var(--mm-ink-3)", marginLeft: 8 }}>{item.detail}</span>
            </div>
            {!item.ok && item.action === "settings:models" && (
              <button
                type="button"
                className="mm-btn mm-btn-sm"
                onClick={onJumpToSettings}
              >
                Open Settings
              </button>
            )}
            {!item.ok && item.action === "people:onboarding" && (
              <button
                type="button"
                className="mm-btn mm-btn-sm"
                onClick={onJumpToPeople}
              >
                Set identity
              </button>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

function InboxScreen({
  files,
  uploading,
  uploadProgress,
  inputRef,
  inboxPath,
  templates,
  fileTemplates,
  onFileTemplateChange,
  onUpload,
  onRefresh,
  onIngest,
  onDelete,
  setupStatus,
  onJumpToSettings,
  onJumpToPeople,
}: {
  files: InboxFile[];
  uploading: boolean;
  uploadProgress: { name: string; loaded: number; total: number } | null;
  inputRef: React.RefObject<HTMLInputElement | null>;
  inboxPath: string;
  templates: TemplateOption[];
  fileTemplates: Record<string, string>;
  onFileTemplateChange: (filename: string, template: string) => void;
  onUpload: (file: File) => Promise<void>;
  onRefresh: () => Promise<void>;
  onIngest: () => Promise<void>;
  onDelete: (file: InboxFile) => void;
  setupStatus: SetupStatus | null;
  onJumpToSettings: () => void;
  onJumpToPeople: () => void;
}) {
  const supportedCount = files.filter((file) => file.supported).length;
  const totalBytes = files.reduce((acc, file) => acc + file.size_bytes, 0);
  const allTemplatesPicked = files
    .filter((file) => file.supported)
    .every((file) => fileTemplates[file.name]);
  const [isDragOver, setDragOver] = useState(false);

  return (
    <section className="mm-screen">
      <div className="mm-screen-header">
        <div>
          <div className="mm-lbl-strong">02 · Intake</div>
          <h1>
            The <Ital>inbox</Ital>
          </h1>
          <p className="mm-screen-sub">
            Drop audio here, or let MeetingMind watch your local inbox folder. Files stay where they are.
          </p>
          <SetupChecklist
            status={setupStatus}
            onJumpToSettings={onJumpToSettings}
            onJumpToPeople={onJumpToPeople}
          />
        </div>
        <div className="mm-screen-actions">
          <button type="button" className="mm-btn" onClick={() => void onRefresh()}>
            ↻ Refresh
          </button>
          <button
            type="button"
            className="mm-btn mm-btn-primary"
            disabled={!supportedCount || !allTemplatesPicked}
            title={
              !supportedCount
                ? "No supported files in the watch folder yet."
                : !allTemplatesPicked
                  ? "Choose a meeting type for each file before ingesting."
                  : `Ingest ${supportedCount} file${supportedCount === 1 ? "" : "s"}.`
            }
            onClick={() => void onIngest()}
          >
            ⌥ Ingest {supportedCount || ""}
          </button>
        </div>
      </div>
      <hr className="mm-rule" style={{ margin: "0 0 28px" }} />
      <div className="mm-inbox-grid">
        <div
          className={isDragOver ? "mm-inbox-drop is-dragover" : "mm-inbox-drop"}
          onDragOver={(event) => {
            if (uploading) return;
            event.preventDefault();
            event.dataTransfer.dropEffect = "copy";
            if (!isDragOver) setDragOver(true);
          }}
          onDragLeave={(event) => {
            // Only clear if leaving the drop element itself, not bubbling
            // from a child element entering.
            if (event.currentTarget === event.target) setDragOver(false);
          }}
          onDrop={(event) => {
            event.preventDefault();
            setDragOver(false);
            if (uploading) return;
            const file = event.dataTransfer.files?.[0];
            if (file) void onUpload(file);
          }}>
          <div className="mm-arches">
            <div className="mm-arch" style={{ height: 56 }} />
            <div className="mm-arch" style={{ height: 70 }} />
            <div className="mm-arch" style={{ height: 56 }} />
          </div>
          <h3>
            Add a <Ital>recording</Ital>
          </h3>
          <p>Drag an audio file onto this card, or choose one from disk. Nothing leaves your machine.</p>
          <input
            ref={inputRef}
            type="file"
            style={{ display: "none" }}
            accept="audio/*,video/mp4,.m4a,.mp3,.wav,.aac,.flac,.mp4"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void onUpload(file);
            }}
          />
          <button
            type="button"
            className="mm-btn mm-btn-primary"
            disabled={uploading}
            onClick={() => inputRef.current?.click()}
          >
            ↑ {uploading ? "Uploading" : "New ingest"}
          </button>
          {uploadProgress && (
            <div style={{ marginTop: 12, width: "100%", maxWidth: 360 }}>
              <div
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  fontSize: 12,
                  marginBottom: 4,
                }}
              >
                <span title={uploadProgress.name}>
                  {uploadProgress.name.length > 30
                    ? uploadProgress.name.slice(0, 27) + "…"
                    : uploadProgress.name}
                </span>
                <span>
                  {uploadProgress.total
                    ? `${Math.round((uploadProgress.loaded / uploadProgress.total) * 100)}%`
                    : "…"}
                  {" · "}
                  {formatBytes(uploadProgress.loaded)} / {formatBytes(uploadProgress.total)}
                </span>
              </div>
              <div
                style={{
                  height: 6,
                  borderRadius: 3,
                  background: "var(--mm-bone-3)",
                  overflow: "hidden",
                }}
              >
                <div
                  style={{
                    height: "100%",
                    width: uploadProgress.total
                      ? `${(uploadProgress.loaded / uploadProgress.total) * 100}%`
                      : "0%",
                    background: "var(--mm-clay, #c8ff5b)",
                    transition: "width 120ms linear",
                  }}
                />
              </div>
              {uploadProgress.loaded >= uploadProgress.total && uploadProgress.total > 0 && (
                <small style={{ display: "block", marginTop: 4, opacity: 0.7 }}>
                  Upload complete — running ingest…
                </small>
              )}
            </div>
          )}
          <div className="mm-inbox-formats">
            {[".m4a", ".mp3", ".wav", ".opus", ".flac"].map((ext) => (
              <span key={ext} className="mm-pill" style={{ fontSize: 11 }}>
                {ext}
              </span>
            ))}
          </div>
        </div>

        <div>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "flex-end" }}>
            <div>
              <h3 className="mm-display" style={{ fontSize: 30, margin: 0 }}>
                Pending files
              </h3>
              <div className="mm-lbl" style={{ marginTop: 4 }}>
                {supportedCount} ready to ingest · {formatBytes(totalBytes)}
              </div>
            </div>
          </div>
          <div className="mm-inbox-list">
            {files.map((file) => {
              const fileTemplate = fileTemplates[file.name] || "";
              return (
                <div
                  key={file.path}
                  className={
                    file.supported
                      ? fileTemplate
                        ? "mm-inbox-row"
                        : "mm-inbox-row needs-template"
                      : "mm-inbox-row is-unsupported"
                  }
                >
                  <div className="mm-inbox-icon">♪</div>
                  <div className="mm-inbox-row-body">
                    <div style={{ fontSize: 14, fontWeight: 500, color: "var(--mm-ink)" }}>
                      {file.name}
                    </div>
                    <div className="mm-lbl" style={{ marginTop: 3 }}>
                      {formatDateTime(new Date(file.modified_at * 1000).toISOString())} ·{" "}
                      {formatBytes(file.size_bytes)}
                    </div>
                  </div>
                  {file.supported ? (
                    <select
                      className="mm-input mm-input-square mm-inbox-template"
                      value={fileTemplate}
                      onChange={(event) =>
                        onFileTemplateChange(file.name, event.target.value)
                      }
                      aria-label={`Meeting type for ${file.name}`}
                    >
                      <option value="" disabled>
                        Pick meeting type…
                      </option>
                      {templates.map((option) => (
                        <option key={option.id} value={option.id}>
                          {option.name}
                        </option>
                      ))}
                    </select>
                  ) : (
                    <span className="mm-mono" style={{ fontSize: 12, color: "var(--mm-ink-2)" }}>
                      unsupported
                    </span>
                  )}
                  <button
                    type="button"
                    className="mm-btn mm-btn-danger mm-btn-sm"
                    onClick={() => void onDelete(file)}
                    aria-label={`Remove ${file.name} from inbox`}
                    title="Remove from inbox"
                  >
                    ✕
                  </button>
                </div>
              );
            })}
            {!files.length && (
              <p className="mm-empty" style={{ marginTop: 14 }}>
                No pending files in the inbox.
              </p>
            )}
          </div>
          <div className="mm-inbox-foot">
            <span className="mm-mono">{inboxPath || "/inbox"}</span>
            <span style={{ display: "inline-flex", gap: 6, alignItems: "center" }}>
              <LiveDot label="watching" />
            </span>
          </div>
        </div>
      </div>
    </section>
  );
}

// ── Review screen (queue rail + detail with 3 tabs) ───────────────────────
function ReviewScreen({
  meetings,
  detail,
  selectedMeetingId,
  onSelectMeeting,
  reviewTab,
  setReviewTab,
  transcriptMode,
  setTranscriptMode,
  summary,
  readyForPromotion,
  obsidianAvailable,
  onSaveSummary,
  onPromote,
  onExportPdf,
  onExportHtml,
  query,
  setQuery,
  filteredSegments,
  segmentById,
  candidates,
  keyTerms,
  ownerTerms,
  showConfidenceChips,
  speakerNumberOf,
  speakerDisplayNameById,
  speakerAliasById,
  chapters,
  activeSegmentId,
  setActiveSegmentId,
  onCorrectSegment,
  onEditSpeaker,
  onRunAudioAlternatives,
  onAcceptAlternative,
  onAfterSegmentRevert,
  onDeleteMeeting,
  templates,
  onSetTemplate,
  onRenameMeeting,
}: {
  meetings: Meeting[];
  detail: MeetingDetail | null;
  selectedMeetingId: number | null;
  onSelectMeeting: (id: number) => void;
  reviewTab: ReviewTab;
  setReviewTab: (tab: ReviewTab) => void;
  transcriptMode: boolean;
  setTranscriptMode: (value: boolean) => void;
  summary: string;
  readyForPromotion: boolean;
  obsidianAvailable: boolean;
  onSaveSummary: (value: string) => void;
  onPromote: () => void;
  onExportPdf: () => void;
  onExportHtml: () => void;
  query: string;
  setQuery: (value: string) => void;
  filteredSegments: Segment[];
  segmentById: Map<number, Segment>;
  candidates: TranscriptCandidate[];
  keyTerms: string[];
  ownerTerms: string[];
  showConfidenceChips: boolean;
  speakerNumberOf: (id: string) => number;
  speakerDisplayNameById: Map<string, string>;
  speakerAliasById: Map<string, string>;
  chapters: Chapter[];
  activeSegmentId: number | null;
  setActiveSegmentId: (id: number | null) => void;
  onCorrectSegment: (segment: Segment, text: string) => void;
  onEditSpeaker: (segment: Segment) => void;
  onRunAudioAlternatives: () => Promise<void>;
  onAcceptAlternative: (candidate: TranscriptCandidate) => Promise<void>;
  onAfterSegmentRevert: () => void;
  onDeleteMeeting: (id: number, title: string) => Promise<void>;
  templates: TemplateOption[];
  onSetTemplate: (meetingId: number, templateId: string) => Promise<void>;
  onRenameMeeting: (meetingId: number, title: string) => Promise<void>;
}) {
  return (
    <div className="mm-review-split">
      <aside className="mm-review-rail">
        <div className="mm-lbl-strong">Queue · {String(meetings.length).padStart(2, "0")}</div>
        <h2>
          The <Ital>review</Ital>
        </h2>
        <div className="mm-lbl" style={{ marginTop: 6 }}>
          {meetings.length} on hand
        </div>
        <hr className="mm-rule" style={{ margin: "18px 0" }} />
        {meetings.map((meeting) => (
          <MeetingCard
            key={meeting.id}
            meeting={meeting}
            isSelected={meeting.id === selectedMeetingId}
            onClick={() => onSelectMeeting(meeting.id)}
            onDelete={() => onDeleteMeeting(meeting.id, meeting.title)}
          />
        ))}
        {!meetings.length && (
          <p className="mm-empty">drop audio into the inbox, then ingest.</p>
        )}
      </aside>

      <div className="mm-review-detail">
        {!detail ? (
          <p className="mm-empty mm-empty-center">
            Select or process a meeting to review the generated note output.
          </p>
        ) : transcriptMode ? (
          <TranscriptView
            detail={detail}
            query={query}
            setQuery={setQuery}
            filteredSegments={filteredSegments}
            segmentById={segmentById}
            candidates={candidates}
            keyTerms={keyTerms}
            ownerTerms={ownerTerms}
            showConfidenceChips={showConfidenceChips}
            speakerNumberOf={speakerNumberOf}
            speakerDisplayNameById={speakerDisplayNameById}
            chapters={chapters}
            activeSegmentId={activeSegmentId}
            setActiveSegmentId={setActiveSegmentId}
            onBack={() => setTranscriptMode(false)}
            onCorrectSegment={onCorrectSegment}
            onEditSpeaker={onEditSpeaker}
            onRunAudioAlternatives={onRunAudioAlternatives}
            onAcceptAlternative={onAcceptAlternative}
            onAfterSegmentRevert={onAfterSegmentRevert}
          />
        ) : (
          <MeetingDetailView
            detail={detail}
            reviewTab={reviewTab}
            setReviewTab={setReviewTab}
            summary={summary}
            readyForPromotion={readyForPromotion}
            obsidianAvailable={obsidianAvailable}
            onSaveSummary={onSaveSummary}
            onPromote={onPromote}
            onExportPdf={onExportPdf}
            onExportHtml={onExportHtml}
            onOpenTranscript={() => {
              setReviewTab("transcript");
              setTranscriptMode(true);
            }}
            onJumpToSegment={(segmentId) => {
              // The existing activeSegmentId effect (search for
              // `scrollIntoView` in this file) scrolls the transcript
              // row into view on change, so flipping the view + the id
              // is all we need to do here.
              setActiveSegmentId(segmentId);
              setReviewTab("transcript");
              setTranscriptMode(true);
            }}
            onEditSpeaker={onEditSpeaker}
            speakerNumberOf={speakerNumberOf}
            speakerDisplayNameById={speakerDisplayNameById}
            speakerAliasById={speakerAliasById}
            chapters={chapters}
            onDelete={() => onDeleteMeeting(detail.meeting.id, detail.meeting.title)}
            templates={templates}
            onSetTemplate={onSetTemplate}
            onRenameMeeting={onRenameMeeting}
          />
        )}
      </div>
    </div>
  );
}

function MeetingCard({
  meeting,
  isSelected,
  onClick,
  onDelete,
}: {
  meeting: Meeting;
  isSelected: boolean;
  onClick: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      className={isSelected ? "mm-meeting-card is-selected" : "mm-meeting-card"}
      onClick={onClick}
      role="button"
      tabIndex={0}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onClick();
        }
      }}
    >
      <div className="mm-meeting-card-top">
        <div className="mm-lbl">{formatDateTime(meeting.created_at)}</div>
        <button
          type="button"
          className="mm-card-delete"
          onClick={(event) => {
            event.stopPropagation();
            onDelete();
          }}
          aria-label={`Delete ${meeting.title}`}
          title="Delete meeting"
        >
          ✕
        </button>
      </div>
      <div className="mm-meeting-title">{meeting.title}</div>
      <div className="mm-meeting-card-pills">
        <Pill tone="quiet">{Math.max(1, Math.round(meeting.duration_seconds / 60))} min</Pill>
        {/* "transcribed" is the default state of every processed meeting in
         * the queue — surfacing it as a pill just adds visual noise. Only
         * the actionable states ('ingested' = still being processed,
         * 'promoted' = sent to Obsidian) get a pill. */}
        {meeting.status !== "transcribed" && (
          <Pill tone={statusTone(meeting.status)}>{statusLabel(meeting.status)}</Pill>
        )}
      </div>
    </div>
  );
}

function statusTone(status: string): "clay" | "sage" | "quiet" {
  if (status === "promoted") return "sage";
  if (status === "ready" || status === "Ready for approval") return "clay";
  return "quiet";
}

function statusLabel(status: string): string {
  // "promoted" is internal jargon — the user-facing meaning is "we wrote
  // this to your Obsidian vault." Surface the meaning, not the code.
  if (status === "promoted") return "Sent to Obsidian";
  if (status === "ingested") return "Awaiting transcribe";
  return status;
}

// ── Meeting detail (3-tab structured layout) ─────────────────────────────────
function MeetingDetailView({
  detail,
  reviewTab,
  setReviewTab,
  summary,
  readyForPromotion,
  obsidianAvailable,
  onSaveSummary,
  onPromote,
  onExportPdf,
  onExportHtml,
  onOpenTranscript,
  onJumpToSegment,
  onEditSpeaker,
  speakerNumberOf,
  speakerDisplayNameById,
  speakerAliasById: _speakerAliasById,
  chapters,
  onDelete,
  templates,
  onSetTemplate,
  onRenameMeeting,
}: {
  detail: MeetingDetail;
  reviewTab: ReviewTab;
  setReviewTab: (tab: ReviewTab) => void;
  summary: string;
  readyForPromotion: boolean;
  obsidianAvailable: boolean;
  onSaveSummary: (value: string) => void;
  onPromote: () => void;
  onExportPdf: () => void;
  onExportHtml: () => void;
  onOpenTranscript: () => void;
  // Click-through from a Reflections evidence pill (and any future
  // "jump to segment" affordance) — opens the transcript view and
  // scrolls to the cited segment id.
  onJumpToSegment: (segmentId: number) => void;
  onEditSpeaker: (segment: Segment) => void;
  speakerNumberOf: (id: string) => number;
  speakerDisplayNameById: Map<string, string>;
  speakerAliasById: Map<string, string>;
  chapters: Chapter[];
  onDelete: () => void;
  templates: TemplateOption[];
  onSetTemplate: (meetingId: number, templateId: string) => Promise<void>;
  onRenameMeeting: (meetingId: number, title: string) => Promise<void>;
}) {
  const overview = detail.overview;
  const alreadyPromoted = overview.status === "promoted";
  const promoteDisabled = !readyForPromotion || alreadyPromoted;
  const speakerIds = Array.from(
    new Set(detail.segments.map((segment) => segment.diarization_speaker_id)),
  );

  // Reflections (experimental, owner-only) — see
  // docs/design/meeting-output-improvements.md §4 and
  // components/ReflectionsPanel.tsx. The endpoint returns 404 when
  // the feature flag is off; we treat that as "hide the tab entirely"
  // rather than rendering an empty surface. Any other error also
  // hides the tab — Reflections is an enhancement, not a must-have.
  const [reflections, setReflections] = useState<Reflections | null>(null);
  const [reflectionsAvailable, setReflectionsAvailable] = useState<boolean | null>(
    null,
  );
  const meetingId = detail.meeting.id;
  const fetchReflections = React.useCallback(async () => {
    try {
      const data = await api.get<Reflections>(
        `/api/meetings/${meetingId}/reflections`,
      );
      setReflections(data);
      setReflectionsAvailable(true);
    } catch {
      setReflections(null);
      setReflectionsAvailable(false);
    }
  }, [meetingId]);
  useEffect(() => {
    let cancelled = false;
    api
      .get<Reflections>(`/api/meetings/${meetingId}/reflections`)
      .then((data) => {
        if (!cancelled) {
          setReflections(data);
          setReflectionsAvailable(true);
        }
      })
      .catch(() => {
        if (!cancelled) {
          setReflections(null);
          setReflectionsAvailable(false);
        }
      });
    return () => {
      cancelled = true;
    };
  }, [meetingId]);
  const reflectionsObservationCount = reflections?.observations.length ?? 0;

  return (
    <>
      <div className="mm-review-hero">
        <div style={{ flex: 1, minWidth: 0 }}>
          {/* Hero is now just: pill (when actionable) + editable title.
           * The slug eyebrow + date/duration meta line were redundant
           * with the meta strip below. Slug is internal plumbing and
           * doesn't belong above the title. */}
          {(readyForPromotion || alreadyPromoted) && (
            <div style={{ marginBottom: 10 }}>
              {alreadyPromoted ? (
                <Pill tone="sage" dot>
                  Sent to Obsidian
                </Pill>
              ) : (
                <Pill tone="clay" dot>
                  Ready to send
                </Pill>
              )}
            </div>
          )}
          <EditableTitle
            value={overview.title}
            onSave={async (next) => {
              await onRenameMeeting(detail.meeting.id, next);
            }}
            render={(text) => <h1>{accentHeadline(text)}</h1>}
          />
        </div>
        <div className="mm-review-actions">
          {/* Transcript button removed from the hero — it's the third tab
           * below the meta strip, so a duplicate top button is friction.
           * Send-to-Obsidian also lives in the Export menu now; the
           * standalone button was redundant. */}
          <ExportMenu
            onPdf={onExportPdf}
            onHtml={onExportHtml}
            onSendToObsidian={alreadyPromoted ? undefined : onPromote}
            sendDisabled={promoteDisabled}
            sendObsidianAvailable={obsidianAvailable}
            alreadySent={alreadyPromoted}
          />
          <OverflowMenu
            items={[
              templates.length > 0
                ? {
                    kind: "template",
                    label: "Change template",
                    templates,
                    value: detail.meeting.template || "general",
                    onPick: (id: string) => void onSetTemplate(detail.meeting.id, id),
                  }
                : null,
              {
                kind: "danger",
                label: "✕ Delete meeting",
                onClick: onDelete,
              },
            ]}
          />
        </div>
      </div>
      <hr className="mm-rule" style={{ margin: "26px 0 0" }} />

      {/* Meta strip — single source of truth for Conducted + Duration.
       * Voices live in their own full-width chip strip on the Mind Map
       * tab (clickable, opens speaker edit). Source filename is internal
       * plumbing — shown in the Quality Audit panel for power users only. */}
      {overview.executive_recap ? (
        <div className="mm-meta-line">
          <span>{formatDate(overview.created_at)}</span>
          <span className="mm-meta-sep" aria-hidden="true">·</span>
          <span>
            {Math.max(1, Math.round(overview.duration_seconds / 60))} min
          </span>
          <span className="mm-meta-sep" aria-hidden="true">·</span>
          <span>
            {overview.participants.length}{" "}
            {overview.participants.length === 1 ? "voice" : "voices"}
          </span>
          <span className="mm-meta-sep" aria-hidden="true">·</span>
          <span>
            {overview.actions.length}{" "}
            {overview.actions.length === 1 ? "action" : "actions"}
          </span>
          <span className="mm-meta-sep" aria-hidden="true">·</span>
          <span>
            {overview.decisions.length}{" "}
            {overview.decisions.length === 1 ? "decision" : "decisions"}
          </span>
        </div>
      ) : (
      <div className="mm-meta-strip mm-meta-strip-slim">
        <div>
          <div className="mm-lbl">Conducted</div>
          <div className="mm-meta-val">{formatDate(overview.created_at)}</div>
          <div className="mm-meta-sub">{formatTime(overview.created_at)}</div>
        </div>
        <div>
          <div className="mm-lbl">Duration</div>
          <div className="mm-meta-val">
            {Math.max(1, Math.round(overview.duration_seconds / 60))} min
          </div>
          <div className="mm-meta-sub">
            {overview.participants.length}{" "}
            {overview.participants.length === 1 ? "voice" : "voices"}
          </div>
        </div>
        <div>
          <div className="mm-lbl">Actions</div>
          <div className="mm-meta-val">
            {overview.actions.length}{" "}
            {overview.actions.length === 1 ? "item" : "items"}
          </div>
          <div className="mm-meta-sub">
            {/* v0.2.11: stat-card subtitles only render real content
                — "extract pending…" was system status leaking into
                the meeting view. When there's no real subtitle to
                show, we render an empty string so the layout
                doesn't shift. */}
            {overview.your_action_count
              ? `${overview.your_action_count} assigned to you`
              : overview.actions.length
                ? "none assigned to you"
                : " "}
          </div>
        </div>
        <div>
          <div className="mm-lbl">Topics</div>
          <div className="mm-meta-val">
            {overview.workstreams.length}{" "}
            {overview.workstreams.length === 1 ? "named" : "named"}
          </div>
          <div className="mm-meta-sub">
            {overview.themes && overview.themes.length
              ? overview.themes[0]
              : overview.workstreams[0]
                ? overview.workstreams[0]
                : " "}
          </div>
        </div>
      </div>
      )}

      <div className="mm-tabs">
        <button
          type="button"
          className={reviewTab === "summary" ? "mm-tab is-active" : "mm-tab"}
          onClick={() => setReviewTab("summary")}
        >
          ✺ Mind map
        </button>
        <button
          type="button"
          className={reviewTab === "minutes" ? "mm-tab is-active" : "mm-tab"}
          onClick={() => setReviewTab("minutes")}
        >
          ☱ Minutes
        </button>
        <button
          type="button"
          className={reviewTab === "transcript" ? "mm-tab is-active" : "mm-tab"}
          onClick={() => {
            setReviewTab("transcript");
            onOpenTranscript();
          }}
        >
          ⌕ Transcript
        </button>
        {reflectionsAvailable && (
          <button
            type="button"
            className={
              reviewTab === "reflections" ? "mm-tab is-active" : "mm-tab"
            }
            onClick={() => setReviewTab("reflections")}
          >
            ✦ Reflections
            {reflectionsObservationCount > 0 && (
              <span className="mm-tab-pill">{reflectionsObservationCount}</span>
            )}
          </button>
        )}
      </div>

      {reviewTab === "summary" && (
        <SummaryMindmap
          overview={overview}
          summary={summary || overview.summary}
          onSaveSummary={onSaveSummary}
          speakerNumberOf={speakerNumberOf}
          speakerIds={speakerIds}
          speakerDisplayNameById={speakerDisplayNameById}
          chapters={chapters}
          onOpenSpeakerEdit={(speakerId) => {
            // Find the first segment matching this speaker and open the
            // existing SpeakerEditModal at the App level.
            const segment = detail.segments.find(
              (s) => s.diarization_speaker_id === speakerId,
            );
            if (segment) onEditSpeaker(segment);
          }}
          onJumpToSegment={onJumpToSegment}
        />
      )}

      {reviewTab === "minutes" && (
        <MinutesView
          overview={overview}
          chapters={chapters}
          segments={detail.segments}
          speakerDisplayNameById={speakerDisplayNameById}
          speakerNumberOf={speakerNumberOf}
        />
      )}

      {reviewTab === "reflections" && reflections && (
        <ReflectionsPanel
          meetingId={meetingId}
          reflections={reflections}
          onReload={fetchReflections}
          onJumpToSegment={onJumpToSegment}
        />
      )}

    </>
  );
}

// ── Summary view (mind-map + sections, structured layout) ────────────────────
function SummaryMindmap({
  overview,
  summary,
  onSaveSummary,
  speakerNumberOf,
  speakerIds,
  speakerDisplayNameById,
  chapters,
  onOpenSpeakerEdit,
  onJumpToSegment,
}: {
  overview: MeetingOverview;
  summary: string;
  onSaveSummary: (value: string) => void;
  speakerNumberOf: (id: string) => number;
  speakerIds: string[];
  speakerDisplayNameById: Map<string, string>;
  chapters: Chapter[];
  onOpenSpeakerEdit: (speakerId: string) => void;
  onJumpToSegment?: (segmentId: number) => void;
}) {
  const confidence = chapters.length ? Math.round(averageConfidence(chapters) * 100) : 89;
  const takeawayCount = overview.key_takeaways.length;
  const actionCount = overview.actions.length;
  const decisionCount = overview.decisions.length;

  const heroMetrics = [
    {
      label: "Duration",
      value: `${Math.max(1, Math.round(overview.duration_seconds / 60))}`,
      unit: "min",
    },
    {
      label: "Voices",
      value: String(speakerIds.length).padStart(2, "0"),
      unit: speakerIds.length === 1 ? "speaker" : "speakers",
    },
    {
      label: "Takeaways",
      value: String(takeawayCount).padStart(2, "0"),
      unit: takeawayCount === 1 ? "point" : "points",
    },
    {
      label: "Confidence",
      value: `${confidence}`,
      unit: "%",
    },
  ];

  // Prefer the model-generated tldr; fall back to a sentence-boundary
  // clip of the full summary so old meetings (extracted before the field
  // existed) still get a wire-thin headline.
  const fullSummary = (summary || overview.summary || "").trim();
  // v0.2.11: "Review pending." is the backend's placeholder string when
  // extract hasn't finished. Treat it as no-content rather than
  // surfacing it in the TL;DR card.
  const PENDING_PLACEHOLDERS = new Set(["Review pending.", "Review pending"]);
  const summaryIsPlaceholder = PENDING_PLACEHOLDERS.has(fullSummary);
  const tldr =
    (overview.tldr || "").trim() ||
    (summaryIsPlaceholder ? "" : deriveTldrFromSummary(fullSummary));
  const hasRealSummary = !summaryIsPlaceholder && tldr.length > 0;
  const hasMoreThanTldr = fullSummary.length > tldr.length + 40;
  const yourActions = overview.your_actions || [];
  const otherActionsList = (overview.other_actions ?? overview.actions ?? []).filter(
    (action) => !yourActions.includes(action),
  );
  const allActions = [...yourActions, ...otherActionsList];
  // overview.action_details is index-aligned to overview.actions (the
  // flat string list). Build a map keyed by the formatted action string
  // so GroupedActionList can render "+N related mentions" disclosures
  // on cluster canonicals.
  const actionDetailByText: Record<string, { memberCount: number; supersededDate: string | null }> = {};
  (overview.action_details ?? []).forEach((detail, i) => {
    const key = (overview.actions ?? [])[i];
    if (!key) return;
    const memberCount = detail.cluster_members?.length ?? 0;
    const supersededDate =
      detail.due_date_history && detail.due_date_history.length > 0
        ? detail.due_date_history[0].date
        : null;
    if (memberCount > 0 || supersededDate) {
      actionDetailByText[key] = { memberCount, supersededDate };
    }
  });

  // Topics + Highlights are extracted as JSX consts so they can render
  // either inside the Tier-3 analytics disclosure (when an executive
  // recap is present) or inline after Decisions (legacy meetings, no
  // recap). Without this they'd need to be duplicated in two render
  // sites; the cost would be drift between the two copies.
  const topicsBlock = overview.workstreams.length > 0 && (
    <>
      <SectionRule
        label="Topics"
        count={String(overview.workstreams.length).padStart(2, "0")}
      />
      <ul className="mm-topic-thread-list">
        {overview.workstreams.map((stream, index) => {
          const description =
            overview.workstream_descriptions?.[stream] ||
            deriveTopicFallback(stream, chapters);
          const featured = index < 2;
          return (
            <li
              key={stream}
              className={
                featured
                  ? "mm-topic-thread mm-topic-thread-featured"
                  : "mm-topic-thread"
              }
            >
              <h5 className="mm-topic-thread-name">{stream}</h5>
              <p className="mm-topic-thread-desc">{description}</p>
            </li>
          );
        })}
      </ul>
    </>
  );

  const highlightsBlock = overview.key_takeaways.length > 0 && (
    <>
      <SectionRule
        label="Highlights"
        count={String(overview.key_takeaways.length).padStart(2, "0")}
      />
      <ol className="mm-highlight-list">
        {overview.key_takeaways.slice(0, 8).map((item, index) => (
          <li key={`hl-${index}`}>
            <span className="mm-highlight-num">
              {String(index + 1).padStart(2, "0")}
            </span>
            <span>{renderMentions(item)}</span>
          </li>
        ))}
      </ol>
    </>
  );

  // Sub-header for an Action items / Decisions / Open questions block.
  // When the recap is present the three sub-sections live under one
  // "Commitments" rule above; we use a smaller h5 here so the layout
  // doesn't stack three thick rules. Legacy meetings keep the existing
  // SectionRule treatment (one rule per section).
  const commitmentSubhead = (label: string, count: number) =>
    overview.executive_recap ? (
      <h5 className="mm-commitments-subhead">
        {label}
        <span className="mm-commitments-subhead-count">
          {String(count).padStart(2, "0")}
        </span>
      </h5>
    ) : (
      <SectionRule label={label} count={String(count).padStart(2, "0")} />
    );

  const hasAnyCommitment =
    allActions.length > 0 ||
    decisionCount > 0 ||
    overview.open_questions.length > 0;

  return (
    <>
      {/* TL;DR — single canonical "what happened" surface. Wire-thin
       * headline; the long-form narrative lives in the Discussion section
       * below for readers who want depth. Edit affordance hidden in a
       * tiny inline link rather than a heavy expander. */}
      {/* v0.2.11: only render the TL;DR card when there's a real
          summary to show. Placeholder text ("Review pending.",
          "Summary still generating.") greeted the user with system
          status instead of content — exactly what the meeting view
          shouldn't do. If extract hasn't run, this section just
          disappears; transcript + minutes tabs remain accessible. */}
      {/* TL;DR + Discussion render only when the executive recap is
          absent. With a recap present the recap covers both surfaces —
          showing all three would put the same content on the page
          three times. Legacy meetings extracted before the recap
          feature keep their existing TL;DR / Discussion treatment. */}
      {hasRealSummary && !overview.executive_recap && (
        <section className="mm-tldr">
          <div className="mm-lbl-strong">TL;DR</div>
          <p className="mm-tldr-text">{renderMentions(tldr)}</p>
          <details className="mm-tldr-more">
            <summary>Edit summary</summary>
            <div className="mm-tldr-edit">
              <SummaryEditor summary={fullSummary} onSave={onSaveSummary} />
            </div>
          </details>
        </section>
      )}

      {overview.executive_recap && (
        <ExecutiveRecapSection
          recap={overview.executive_recap}
          resolveBulletAction={(bullet) =>
            matchBulletToActionIndex(
              bullet,
              overview.actions,
              overview.action_details ?? [],
            )
          }
          onJumpToAction={(actionIndex) => {
            // Find the rendered action row by its data attribute and
            // scroll it into view. A transient highlight class signals
            // which row was the target; CSS animates it out after the
            // intersection is read.
            const el = document.querySelector(
              `[data-action-index="${actionIndex}"]`,
            ) as HTMLElement | null;
            if (!el) return;
            // Behavior 'auto' rather than 'smooth' so the scroll lands
            // deterministically. Some Chromium contexts swallow smooth
            // scrolls without a fresh user-activation token; the flash
            // highlight below provides the visual continuity.
            el.scrollIntoView({ behavior: "auto", block: "center" });
            el.classList.add("mm-action-row-flash");
            window.setTimeout(
              () => el.classList.remove("mm-action-row-flash"),
              1800,
            );
          }}
        />
      )}

      {/* Themes — short two-word lenses on this specific meeting. Different
       * from workstreams (cross-meeting threads). Sit just under the
       * TL;DR so readers see the angle before the numbers. */}
      {overview.themes && overview.themes.length > 0 && (
        <div className="mm-theme-row">
          {overview.themes.map((theme) => (
            <span key={theme} className="mm-theme-pill">
              {theme}
            </span>
          ))}
        </div>
      )}

      {/* Voices — full-width chip strip just under the meta data.
       * Duration moved into the top meta-strip so we don't repeat it
       * here. Actions + Decisions counts are visible in the hero metric
       * strip above if present. */}
      <div className="mm-voices-strip">
        <div className="mm-lbl">Voices · {String(speakerIds.length).padStart(2, "0")}</div>
        {speakerIds.length === 0 ? (
          <div className="mm-hero-value">—</div>
        ) : (
          <div className="mm-voice-chip-row" style={{ marginTop: 6 }}>
            {speakerIds.map((id) => {
              const name = speakerDisplayNameById.get(id) || id;
              const num = speakerNumberOf(id);
              return (
                <button
                  key={id}
                  type="button"
                  className="mm-voice-chip"
                  onClick={() => onOpenSpeakerEdit(id)}
                  title={`Edit ${name} · click to assign or open People`}
                >
                  <SpeakerAvatar name={name} speakerNumber={num} size={22} />
                  <span className="mm-voice-chip-name">{name}</span>
                </button>
              );
            })}
          </div>
        )}
      </div>
      {(allActions.length > 0 || decisionCount > 0) && (
        <div className="mm-hero-strip mm-hero-strip-quiet">
          {allActions.length > 0 && (
            <div className="mm-hero-metric">
              <div className="mm-lbl">Actions</div>
              <div className="mm-hero-value">
                {String(allActions.length).padStart(2, "0")}
                <span className="mm-hero-unit">
                  {" "}
                  {allActions.length === 1 ? "item" : "items"}
                </span>
              </div>
            </div>
          )}
          {decisionCount > 0 && (
            <div className="mm-hero-metric">
              <div className="mm-lbl">Decisions</div>
              <div className="mm-hero-value">
                {String(decisionCount).padStart(2, "0")}
                <span className="mm-hero-unit">
                  {" "}
                  {decisionCount === 1 ? "made" : "made"}
                </span>
              </div>
            </div>
          )}
        </div>
      )}

      <ForYouSection overview={overview} onJumpToSegment={onJumpToSegment} />

      {/* Tier 3 analytics — health, drivers, stat callouts, tension,
       * topics, highlights. Collapsed by default when an executive
       * recap is present. Legacy meetings keep the existing inline
       * layout because the recap's first-impression role doesn't
       * exist for them; Topics + Highlights render below for legacy
       * via the !overview.executive_recap branch. */}
      {(() => {
        const analyticsBlock = (
          <>
            <MeetingHealthChips
              health={overview.meeting_health}
              centerOfGravity={overview.center_of_gravity}
            />
            <ConversationDriversPanel drivers={overview.conversation_drivers} />
            {/* Stat callouts — big-number highlights from the meeting. */}
            {overview.stat_callouts && overview.stat_callouts.length > 0 && (
              <div className="mm-stat-grid">
                {overview.stat_callouts.map((stat, index) => (
                  <div
                    key={`${stat.label}-${index}`}
                    className="mm-stat-card"
                  >
                    <div className="mm-stat-value">{stat.value}</div>
                    <div className="mm-stat-label">{stat.label}</div>
                  </div>
                ))}
              </div>
            )}
            {/* Tension points — sentiment-split cards when the meeting
             * had two sides. Most meetings have 0-1. */}
            {overview.tension_points &&
              overview.tension_points.length > 0 && (
                <div className="mm-tension-list">
                  {overview.tension_points.map((tension, index) => (
                    <div
                      key={`${tension.title}-${index}`}
                      className="mm-tension-card"
                    >
                      <div className="mm-tension-title">
                        <span aria-hidden="true">⚖</span> {tension.title}
                      </div>
                      <div className="mm-tension-sides">
                        <div className="mm-tension-side mm-tension-positive">
                          <div className="mm-lbl">In favour</div>
                          <p>{renderMentions(tension.positive_side)}</p>
                        </div>
                        <div className="mm-tension-side mm-tension-negative">
                          <div className="mm-lbl">Against</div>
                          <p>{renderMentions(tension.negative_side)}</p>
                        </div>
                      </div>
                    </div>
                  ))}
                </div>
              )}
            {/* Topics + Highlights live inside the disclosure when an
             * executive recap is present. They're rendered inline
             * after Decisions for legacy meetings (below). */}
            {overview.executive_recap && topicsBlock}
            {overview.executive_recap && highlightsBlock}
          </>
        );
        return overview.executive_recap ? (
          <details className="mm-analytics-disclosure">
            <summary>Meeting analytics</summary>
            <div className="mm-analytics-body">{analyticsBlock}</div>
          </details>
        ) : (
          analyticsBlock
        );
      })()}

      {/* Commitments rule when an executive recap is present.
       * Replaces the per-section SectionRules below with lighter h5
       * sub-headers (see commitmentSubhead) so the layout doesn't
       * stack three thick rules under each commitment kind. */}
      {overview.executive_recap && hasAnyCommitment && (
        <SectionRule label="Commitments" />
      )}

      {/* Action items — only render the section at all when there are
          actions. v0.2.11: dropped the "No actions captured" empty
          state because it was greeting the user with system status
          rather than content. */}
      {allActions.length > 0 && (
        <>
          {commitmentSubhead("Action items", allActions.length)}
          <GroupedActionList
            yourActions={yourActions}
            otherActionsList={otherActionsList}
            actionOwnerOf={(action) => extractActionOwner(action)}
            clusterInfoOf={(action) => actionDetailByText[action]}
            actionIndexOf={(action) => {
              const idx = overview.actions.indexOf(action);
              return idx >= 0 ? idx : undefined;
            }}
          />
        </>
      )}

      {/* Decisions — collapses entirely when empty so empty sections don't
       * dilute the page. */}
      {decisionCount > 0 && (
        <>
          {commitmentSubhead("Decisions", decisionCount)}
          <ul className="mm-decision-list">
            {overview.decisions.map((decision, index) => {
              const rationale =
                overview.decision_details?.[index]?.rationale ?? null;
              return (
                <li key={`${decision}-${index}`}>
                  <span className="mm-action-bullet" aria-hidden="true">◆</span>
                  <div>
                    <span>{renderMentions(decision)}</span>
                    {rationale && (
                      <div className="mm-decision-rationale">
                        <span className="mm-rationale-label">why:</span>{" "}
                        {renderMentions(rationale)}
                      </div>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}

      {/* Topics + Highlights — rendered here inline only for legacy
       * meetings (no recap). With a recap, both live inside the
       * Tier-3 analytics disclosure above so the page leads with
       * Commitments rather than meeting analytics. */}
      {!overview.executive_recap && topicsBlock}
      {!overview.executive_recap && highlightsBlock}

      {/* Discussion — the long-form narrative summary brought out from
       * behind the "Read more" expander. Multi-paragraph prose for
       * readers who want depth. Empty when the model returned a short
       * one-paragraph summary (the TL;DR already covered it). Hidden
       * when an executive recap is present — the recap is the prose
       * surface; showing both would re-tell the same meeting twice. */}
      {fullSummary && fullSummary.length > 280 && !overview.executive_recap && (
        <>
          <SectionRule label="Discussion" />
          <div className="mm-discussion">
            {fullSummary.split(/\n\n+|\n(?=[A-Z])/).map((para, index) => (
              <p key={`d-${index}`}>{renderMentions(para.trim())}</p>
            ))}
          </div>
        </>
      )}

      {/* Open questions — quiet section, only when present. */}
      {overview.open_questions.length > 0 && (
        <>
          {commitmentSubhead("Open questions", overview.open_questions.length)}
          <ul className="mm-decision-list">
            {overview.open_questions.slice(0, 6).map((question, index) => {
              const detail = overview.open_question_details?.[index];
              const status = detail?.status ?? "unanswered";
              const pillLabel =
                status === "partially_answered"
                  ? "partial"
                  : status === "deferred"
                  ? "deferred"
                  : null;
              return (
                <li key={`${question}-${index}`}>
                  <span className="mm-action-bullet" aria-hidden="true">?</span>
                  <div>
                    <span>{renderMentions(question)}</span>
                    {pillLabel && (
                      <span className={`mm-oq-status mm-oq-status-${status}`}>
                        {pillLabel}
                      </span>
                    )}
                    {detail?.raised_by && (
                      <span className="mm-oq-attribution">
                        raised by {detail.raised_by}
                      </span>
                    )}
                  </div>
                </li>
              );
            })}
          </ul>
        </>
      )}

      {/* Quality audit — confidence, ASR repair flags, speaker review state.
       * Collapsed by default; only opens when a power user is auditing the
       * pipeline's output. Replaces the always-on confidence pills + count
       * chips that distracted normal readers. */}
      <details className="mm-audit">
        <summary>Quality audit · {confidence}% confidence</summary>
        <div className="mm-audit-grid">
          <div>
            <div className="mm-lbl">Content confidence</div>
            <div className="mm-meta-val">{confidence}%</div>
            <div className="mm-meta-sub">Averaged across all transcript segments.</div>
          </div>
          <div>
            <div className="mm-lbl">Speaker review</div>
            <div className="mm-meta-val">
              {overview.speaker_status === "complete" ? "Complete" : "Pending"}
            </div>
            <div className="mm-meta-sub">
              {overview.speaker_status === "complete"
                ? "All speakers named."
                : "Some speakers still need confirmation."}
            </div>
          </div>
          <div>
            <div className="mm-lbl">Voices</div>
            <div className="mm-meta-val">{String(speakerIds.length).padStart(2, "0")}</div>
            <div className="mm-meta-sub">
              {speakerIds
                .map((id) => speakerDisplayNameById.get(id) || id)
                .join(" · ")}
            </div>
          </div>
          <div>
            <div className="mm-lbl">Source</div>
            <div
              className="mm-meta-val"
              style={{ fontSize: 14, wordBreak: "break-all" }}
            >
              {overview.source_file || "—"}
            </div>
            <div className="mm-meta-sub">kept locally</div>
          </div>
        </div>
      </details>
    </>
  );
}

function deriveTldrFromSummary(summary: string): string {
  // Clip at the first sentence boundary after ~200 chars, capped at 320.
  // The full summary still lives behind the "Read more" expander, so this
  // fallback just needs to read as a real first-glance sentence.
  if (!summary) return "";
  if (summary.length <= 280) return summary;
  const window = summary.slice(0, 320);
  const lastStop = Math.max(
    window.lastIndexOf(". "),
    window.lastIndexOf("! "),
    window.lastIndexOf("? "),
  );
  if (lastStop > 120) return window.slice(0, lastStop + 1).trim();
  // Fallback: clip at the last space before 280.
  const lastSpace = summary.slice(0, 280).lastIndexOf(" ");
  return summary.slice(0, lastSpace > 0 ? lastSpace : 280).trim() + "…";
}

function extractActionOwner(action: string): string {
  // Parse "@Avery: ..." or "Avery Smith — ..." or fall through to "Unassigned".
  // Owner strings come from the model with varied formatting; this is a
  // best-effort group key, not a strict parser. We match a first name
  // (and optional last name), each token starting with an uppercase
  // letter and built from word chars / hyphens / apostrophes.
  //
  // Notes on edge cases this guards against:
  // - "@Avery to send notes" used to capture "Avery to send notes" (the
  //   regex allowed spaces in the class). Now we only consume a name
  //   token, then optional second name token.
  // - "Avery: ship the deck" matches the dash branch.
  // - "Ship the deck by Friday" returns "Unassigned" (no leading name).
  const atMatch = action.match(ACTION_OWNER_AT_RE);
  if (atMatch) return atMatch[1].trim();
  const dashMatch = action.match(ACTION_OWNER_DASH_RE);
  if (dashMatch) return dashMatch[1].trim();
  return "Unassigned";
}

// Hoisted to module scope (audit perf NIT): `extractActionOwner` runs once
// per action item per render, and these regexes were being recompiled each
// call. Cheap fix, measurable on long action lists.
const _NAME_TOK = "[A-Z][A-Za-z0-9'\\-]{0,30}";
const ACTION_OWNER_AT_RE = new RegExp(`^@(${_NAME_TOK}(?:\\s+${_NAME_TOK})?)\\b`);
const ACTION_OWNER_DASH_RE = new RegExp(
  `^(${_NAME_TOK}(?:\\s+${_NAME_TOK})?)\\s*[—:\\-]\\s`,
);

// Minimal inline-markdown renderer: handles **bold** and *italic* only,
// which is all the recap prompt is allowed to emit. Keeps a tight
// surface so we don't ship a full markdown library just for two
// styles, and so any content beyond those two falls through as plain
// text rather than rendering surprise headings.
function renderRecapInlineMarkdown(text: string): React.ReactNode {
  // Split on the bold-or-italic pattern; capture group keeps the tokens.
  const tokens = text.split(/(\*\*[^*]+\*\*|\*[^*]+\*)/g);
  return tokens.map((token, i) => {
    if (token.startsWith("**") && token.endsWith("**")) {
      return <strong key={i}>{token.slice(2, -2)}</strong>;
    }
    if (token.startsWith("*") && token.endsWith("*") && token.length > 2) {
      return <em key={i}>{token.slice(1, -1)}</em>;
    }
    return token;
  });
}

// Token-set overlap between a recap bullet's commitment and an action
// item's text. Used to wire each strategy bullet to the underlying
// action so a click jumps to the operational row. Stopwords/short words
// dropped; same-owner gate is enforced in the caller, not here.
function _commitmentTokenOverlap(a: string, b: string): number {
  const STOP = new Set([
    "the", "a", "an", "and", "or", "to", "for", "of", "in", "on", "at",
    "by", "with", "from", "into", "send", "share", "make", "set", "get",
    "this", "that", "is", "are", "be", "by", "his", "her", "them",
  ]);
  const toks = (s: string) =>
    new Set(
      (s.toLowerCase().match(/[a-z0-9]+/g) || []).filter(
        (w) => w.length > 2 && !STOP.has(w),
      ),
    );
  const ta = toks(a);
  const tb = toks(b);
  if (ta.size === 0 || tb.size === 0) return 0;
  let intersection = 0;
  ta.forEach((t) => {
    if (tb.has(t)) intersection += 1;
  });
  return intersection / (ta.size + tb.size - intersection);
}

// Resolves a recap strategy bullet to the index of its matching action
// in `overview.actions`. Returns null when nothing matches well enough
// — false positives are worse than missing links here.
function matchBulletToActionIndex(
  bullet: { owner: string; commitment: string },
  actions: string[],
  actionDetails: NonNullable<MeetingOverview["action_details"]>,
): number | null {
  if (!bullet.commitment) return null;
  const ownerLower = bullet.owner.toLowerCase();
  let best: { index: number; score: number } | null = null;
  for (let i = 0; i < actions.length; i++) {
    const detail = actionDetails[i];
    if (!detail) continue;
    // Owner gate: compare first-token equality, not startsWith. A
    // startsWith check would collapse "Sam" onto both "Sam Chen" and
    // "Sammy Adams"; comparing the first token after splitting on
    // whitespace keeps those distinct. Pass through when either side
    // is missing so we don't drop matches purely on attribution.
    const actionOwner = (detail.owner_display_name || "").toLowerCase();
    const bulletFirst = ownerLower.split(/\s+/)[0] || "";
    const actionFirst = actionOwner.split(/\s+/)[0] || "";
    if (bulletFirst && actionFirst && bulletFirst !== actionFirst) {
      continue;
    }
    const score = _commitmentTokenOverlap(bullet.commitment, actions[i]);
    if (score >= 0.25 && (best === null || score > best.score)) {
      best = { index: i, score };
    }
  }
  return best ? best.index : null;
}

function ExecutiveRecapSection({
  recap,
  onJumpToAction,
  resolveBulletAction,
}: {
  recap: NonNullable<MeetingOverview["executive_recap"]>;
  onJumpToAction?: (actionIndex: number) => void;
  resolveBulletAction?: (bullet: {
    owner: string;
    commitment: string;
  }) => number | null;
}) {
  // Each section may be null when the recap synthesizer hit its
  // fallback path (no clear reframe, no load-bearing risk). Skip
  // sections with neither header nor body so the layout stays clean.
  const sections: Array<{
    key: string;
    header: string;
    body: string | null;
    bullets?: Array<{ owner: string; commitment: string; purpose: string | null }>;
    trailer?: string | null;
  }> = [];
  if (recap.reframe && (recap.reframe.header || recap.reframe.body)) {
    sections.push({
      key: "reframe",
      header: recap.reframe.header || "",
      body: recap.reframe.body,
    });
  }
  if (
    recap.strategy &&
    (recap.strategy.header || recap.strategy.body || recap.strategy.bullets.length > 0)
  ) {
    sections.push({
      key: "strategy",
      header: recap.strategy.header || "",
      body: recap.strategy.body,
      bullets: recap.strategy.bullets,
      trailer: recap.strategy.trailer,
    });
  }
  if (recap.risk && (recap.risk.header || recap.risk.body)) {
    sections.push({
      key: "risk",
      header: recap.risk.header || "",
      body: recap.risk.body,
    });
  }
  if (sections.length === 0) return null;

  return (
    <section className="mm-recap" aria-label="Executive recap">
      {sections.map((section) => (
        <div key={section.key} className={`mm-recap-block mm-recap-${section.key}`}>
          {section.header && (
            <h4 className="mm-recap-header">{section.header}</h4>
          )}
          {section.body && (
            <p className="mm-recap-body">
              {renderRecapInlineMarkdown(section.body)}
            </p>
          )}
          {section.bullets && section.bullets.length > 0 && (
            <ul className="mm-recap-bullets">
              {section.bullets.map((b, i) => {
                const targetIdx = resolveBulletAction
                  ? resolveBulletAction({ owner: b.owner, commitment: b.commitment })
                  : null;
                const clickable = targetIdx !== null && onJumpToAction;
                const body = (
                  <>
                    <strong>{b.owner}</strong> — {b.commitment}
                    {b.purpose && (
                      <span className="mm-recap-purpose"> ({b.purpose})</span>
                    )}
                  </>
                );
                return (
                  <li key={i}>
                    {clickable ? (
                      <button
                        type="button"
                        className="mm-recap-bullet-link"
                        onClick={() => onJumpToAction(targetIdx)}
                        title="Jump to the underlying action item"
                      >
                        {body}
                      </button>
                    ) : (
                      body
                    )}
                  </li>
                );
              })}
            </ul>
          )}
          {section.trailer && (
            <p className="mm-recap-trailer">
              {renderRecapInlineMarkdown(section.trailer)}
            </p>
          )}
        </div>
      ))}
    </section>
  );
}

function GroupedActionList({
  yourActions,
  otherActionsList,
  actionOwnerOf,
  clusterInfoOf,
  actionIndexOf,
}: {
  yourActions: string[];
  otherActionsList: string[];
  actionOwnerOf: (action: string) => string;
  clusterInfoOf?: (
    action: string,
  ) => { memberCount: number; supersededDate: string | null } | undefined;
  // Returns the original index into overview.actions[] for this action
  // string. Used so recap strategy bullets can jump-to the underlying
  // row by data-action-index attribute. Undefined when not threaded.
  actionIndexOf?: (action: string) => number | undefined;
}) {
  const renderClusterTag = (action: string) => {
    const info = clusterInfoOf?.(action);
    if (!info) return null;
    return (
      <span className="mm-action-cluster-tag" title="Near-duplicate commitments folded into this one">
        {info.memberCount > 0 ? ` +${info.memberCount} related` : ""}
        {info.supersededDate ? ` · was ${info.supersededDate}` : ""}
      </span>
    );
  };
  // Group otherActionsList by owner so readers can scan to their name.
  // Your-actions are kept separate at the top with the existing chartreuse
  // treatment; they're already personalised.
  const groups = new Map<string, string[]>();
  otherActionsList.forEach((action) => {
    const owner = actionOwnerOf(action);
    const bucket = groups.get(owner) ?? [];
    bucket.push(action);
    groups.set(owner, bucket);
  });
  const groupEntries = Array.from(groups.entries()).sort((a, b) => {
    if (a[0] === "Unassigned") return 1;
    if (b[0] === "Unassigned") return -1;
    return a[0].localeCompare(b[0]);
  });
  return (
    <div className="mm-action-groups">
      {yourActions.length > 0 && (
        <div className="mm-action-group mm-action-group-you">
          <div className="mm-action-group-head">
            <span className="mm-pill-yours">⌖</span>
            <span className="mm-action-group-name">@You</span>
            <span className="mm-action-group-count">{yourActions.length}</span>
          </div>
          <ul className="mm-action-list">
            {yourActions.map((action, index) => (
              <li
                key={`yours-${index}-${action.slice(0, 24)}`}
                className="mm-action-row mm-action-yours-row"
                data-action-index={actionIndexOf?.(action) ?? undefined}
              >
                <span className="mm-action-bullet" aria-hidden="true">→</span>
                <span>
                  {renderMentions(action)}
                  {renderClusterTag(action)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      )}
      {groupEntries.map(([owner, items]) => (
        <div key={owner} className="mm-action-group">
          <div className="mm-action-group-head">
            <span className="mm-action-group-name">@{owner}</span>
            <span className="mm-action-group-count">{items.length}</span>
          </div>
          <ul className="mm-action-list">
            {items.map((action, index) => (
              <li
                key={`${owner}-${index}-${action.slice(0, 24)}`}
                className="mm-action-row"
                data-action-index={actionIndexOf?.(action) ?? undefined}
              >
                <span className="mm-action-bullet" aria-hidden="true">→</span>
                <span>
                  {renderMentions(action)}
                  {renderClusterTag(action)}
                </span>
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function deriveTopicFallback(name: string, chapters: Chapter[]): string {
  // Try to find a chapter whose title fuzzy-matches this workstream name; use
  // its first bullet text as a stand-in description for meetings extracted
  // before workstreams gained a `description` field.
  const lower = name.toLowerCase();
  const match = chapters.find(
    (chapter) =>
      chapter.title.toLowerCase().includes(lower) ||
      lower.includes(chapter.title.toLowerCase()),
  );
  const firstBullet = match?.bullets[0]?.text;
  if (firstBullet) return truncate(firstBullet, 180);
  return "Description not yet generated — re-extract this meeting to populate.";
}

function ExportMenu({
  onPdf,
  onHtml,
  onSendToObsidian,
  sendDisabled,
  sendObsidianAvailable,
  alreadySent,
}: {
  onPdf: () => void;
  onHtml: () => void;
  onSendToObsidian?: () => void;
  sendDisabled?: boolean;
  sendObsidianAvailable?: boolean;
  alreadySent?: boolean;
}) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  return (
    <div className="mm-export-menu" ref={wrapRef}>
      <button
        type="button"
        className="mm-btn"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        ⤓ Export <span style={{ opacity: 0.55, marginLeft: 2 }}>▾</span>
      </button>
      {open && (
        <div className="mm-export-menu-pop" role="menu">
          <button
            type="button"
            role="menuitem"
            className="mm-export-menu-item"
            onClick={() => {
              setOpen(false);
              onPdf();
            }}
          >
            <span>PDF</span>
            <small>Two-page printable, hand-rolled</small>
          </button>
          <button
            type="button"
            role="menuitem"
            className="mm-export-menu-item"
            onClick={() => {
              setOpen(false);
              onHtml();
            }}
          >
            <span>HTML</span>
            <small>Standalone page · print for highest fidelity</small>
          </button>
          {onSendToObsidian && (
            <button
              type="button"
              role="menuitem"
              className="mm-export-menu-item"
              disabled={sendDisabled || !sendObsidianAvailable}
              title={
                !sendObsidianAvailable
                  ? "Obsidian isn't installed locally."
                  : sendDisabled
                    ? "Confirm speaker review before sending."
                    : alreadySent
                      ? "Already in your Obsidian vault — clicking again will overwrite the existing note with the latest extraction."
                      : "Write this meeting note to your Obsidian vault."
              }
              onClick={() => {
                setOpen(false);
                onSendToObsidian();
              }}
            >
              <span>
                {!sendObsidianAvailable
                  ? "Send to Obsidian (unavailable)"
                  : alreadySent
                    ? "Re-send to Obsidian"
                    : "Send to Obsidian"}
              </span>
              <small>
                {!sendObsidianAvailable
                  ? "Install Obsidian or configure a vault path first."
                  : alreadySent
                    ? "Overwrites the existing vault note with the latest extraction."
                    : "Writes the canonical meeting note to your vault."}
              </small>
            </button>
          )}
        </div>
      )}
    </div>
  );
}

// ── Overflow menu (•••) — secondary meeting actions that don't deserve a
// permanent button slot in the hero. Currently: change template, delete.
type OverflowItem =
  | { kind: "danger"; label: string; onClick: () => void }
  | {
      kind: "template";
      label: string;
      templates: TemplateOption[];
      value: string;
      onPick: (id: string) => void;
    }
  | null;

function OverflowMenu({ items }: { items: OverflowItem[] }) {
  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (!open) return;
    const onDoc = (event: MouseEvent) => {
      if (!wrapRef.current?.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onDoc);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onDoc);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);
  const visibleItems = items.filter(Boolean) as Exclude<OverflowItem, null>[];
  if (visibleItems.length === 0) return null;
  return (
    <div className="mm-export-menu" ref={wrapRef}>
      <button
        type="button"
        className="mm-btn mm-btn-sm"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label="More actions"
        title="More actions"
      >
        •••
      </button>
      {open && (
        <div className="mm-export-menu-pop" role="menu">
          {visibleItems.map((item, index) => {
            if (item.kind === "template") {
              return (
                <div
                  key={`${item.kind}-${index}`}
                  className="mm-export-menu-item"
                  style={{ flexDirection: "column", alignItems: "flex-start" }}
                >
                  <small style={{ opacity: 0.7, marginBottom: 4 }}>{item.label}</small>
                  <select
                    value={item.value}
                    onChange={(event) => {
                      item.onPick(event.target.value);
                      setOpen(false);
                    }}
                    className="mm-input mm-input-square"
                    style={{ width: "100%" }}
                  >
                    {item.templates.map((opt) => (
                      <option key={opt.id} value={opt.id}>
                        {opt.name}
                      </option>
                    ))}
                  </select>
                </div>
              );
            }
            return (
              <button
                key={`${item.kind}-${index}`}
                type="button"
                role="menuitem"
                className="mm-export-menu-item"
                onClick={() => {
                  setOpen(false);
                  item.onClick();
                }}
                style={{ color: "var(--mm-rust)" }}
              >
                <span>{item.label}</span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

function SynopsisTile({
  summary,
  pullQuote,
}: {
  summary: string;
  pullQuote: { text: string; speakerName: string; startMs: number } | null;
}) {
  const [expanded, setExpanded] = useState(false);
  const fullText = summary || "Run extraction to generate a summary.";
  const TRUNCATE_AT = 360;
  const isLong = fullText.length > TRUNCATE_AT;
  const displayText = expanded || !isLong ? fullText : `${fullText.slice(0, TRUNCATE_AT).trim()}…`;

  return (
    <div className="mm-mindmap-tile" style={{ minHeight: 220 }}>
      <h4>
        <span className="mm-tile-glyph" style={{ color: "var(--mm-clay)" }}>✺</span> Synopsis
      </h4>
      <p>{displayText}</p>
      {isLong && (
        <button
          type="button"
          className="mm-btn mm-btn-ghost mm-btn-sm"
          style={{ alignSelf: "flex-start", padding: "2px 0" }}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Show less" : "Show more"}
        </button>
      )}
      {pullQuote && (
        <div className="mm-pull">
          "{pullQuote.text}"
          {pullQuote.speakerName && (
            <div
              className="mm-lbl"
              style={{ marginTop: 6, fontSize: 9, color: "var(--mm-ink-3)" }}
            >
              — {pullQuote.speakerName} · {formatMs(pullQuote.startMs)}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ForYouTile({ overview }: { overview: MeetingOverview }) {
  const yourActions = overview.your_actions?.length || 0;
  const yourDecisions = overview.your_decisions?.length || 0;
  const yourWorkstreams = overview.your_workstreams?.length || 0;
  const inAttendance = overview.you_in_attendance;
  const headline =
    inAttendance && (yourActions || yourDecisions || yourWorkstreams)
      ? `${yourActions} on you`
      : inAttendance
        ? "You attended"
        : "Not your meeting";
  return (
    <div className="mm-mindmap-stat mm-foryou-stat">
      <div className="mm-lbl mm-foryou-label">For {overview.owner?.display_name || "you"}</div>
      <div className="mm-stat-value">{yourActions}</div>
      <div className="mm-stat-sub">{headline}</div>
      <div className="mm-row" style={{ gap: 6, marginTop: 10, justifyContent: "center", flexWrap: "wrap" }}>
        {yourDecisions > 0 && <Pill tone="sage">{yourDecisions} decisions</Pill>}
        {yourWorkstreams > 0 && <Pill tone="quiet">{yourWorkstreams} workstreams</Pill>}
        {!inAttendance && <Pill tone="quiet">FYI only</Pill>}
      </div>
    </div>
  );
}

function EmptyStat({ label, count, empty }: { label: string; count: number; empty: string }) {
  return (
    <div className="mm-panel" style={{ textAlign: "center" }}>
      <div className="mm-lbl-strong">{label}</div>
      <div
        className="mm-display"
        style={{ fontSize: 72, marginTop: 10, color: count ? "var(--mm-clay)" : "var(--mm-ink-3)" }}
      >
        {count || 0}
      </div>
      {!count && (
        <div
          style={{
            fontFamily: "var(--mm-font-display)",
            fontStyle: "italic",
            fontSize: 13,
            color: "var(--mm-ink-3)",
          }}
        >
          {empty}
        </div>
      )}
    </div>
  );
}

function SummaryEditor({
  summary,
  onSave,
}: {
  summary: string;
  onSave: (value: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(summary);
  useEffect(() => {
    setDraft(summary);
    setEditing(false);
  }, [summary]);

  if (editing) {
    return (
      <div className="mm-tx-editor">
        <textarea value={draft} onChange={(event) => setDraft(event.target.value)} />
        <div className="mm-tx-editor-actions">
          <button
            type="button"
            className="mm-btn"
            onClick={() => {
              setDraft(summary);
              setEditing(false);
            }}
          >
            Cancel
          </button>
          <button
            type="button"
            className="mm-btn mm-btn-primary"
            onClick={() => {
              onSave(draft);
              setEditing(false);
            }}
          >
            ✓ Approve changes
          </button>
        </div>
      </div>
    );
  }

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "1fr auto",
        gap: 18,
        alignItems: "flex-start",
      }}
    >
      <p
        className="mm-body-serif"
        style={{ margin: 0, color: "var(--mm-ink)" }}
      >
        {summary ? `"${summary}"` : "Run extraction to generate a summary."}
      </p>
      <button
        type="button"
        className="mm-btn mm-btn-ghost"
        onClick={() => setEditing(true)}
        style={{ fontSize: 12 }}
      >
        ✎ Edit
      </button>
    </div>
  );
}

// ── Minutes view (structured prose, structured layout) ───────────────────────
function MinutesView({
  overview,
  chapters,
  segments,
  speakerDisplayNameById,
  speakerNumberOf,
}: {
  overview: MeetingOverview;
  chapters: Chapter[];
  // v0.2.10: segments fallback. When extract hasn't produced
  // participant_contributions yet (or it failed), we still want every
  // speaker who actually talked to appear here, not just the one or
  // two the LLM happened to finish before crashing.
  segments: Segment[];
  speakerDisplayNameById: Map<string, string>;
  speakerNumberOf: (id: string) => number;
}) {
  // Two-mode Minutes:
  //   • "person" — per-attendee 1-2 sentence distillation (what each
  //     person brought). Default.
  //   • "time" — chronological `[hh:mm] **bold subhead** + nested
  //     bullets` per chapter marker, in chronological order.
  // Toggle persists in localStorage so a user's preferred shape sticks.
  const [mode, setMode] = useState<"person" | "time">(() => {
    // localStorage access can throw in Safari private mode / embedded
    // webviews. Default to "person" if we can't read.
    try {
      const stored = localStorage.getItem("mm-minutes-mode");
      return stored === "time" ? "time" : "person";
    } catch {
      return "person";
    }
  });
  useEffect(() => {
    try {
      localStorage.setItem("mm-minutes-mode", mode);
    } catch {
      /* ignore storage errors */
    }
  }, [mode]);

  const contributions = overview.participant_contributions || [];
  // v0.2.10: three-tier fallback so the user always sees every
  // speaker who actually spoke, not just the ones a partial extract
  // happened to finish.
  //   1. LLM-extracted participant_contributions (best — distilled)
  //   2. Chapter bullets grouped by topSpeakerId (heuristic)
  //   3. Raw segments grouped by speaker (longest segment per speaker)
  //
  // Use tier 1 as the base; pad with tier 3 for any speaker who isn't
  // already covered. (Partial extracts that produced one or two
  // contributions and crashed shouldn't hide the other 4 speakers.)
  // Tier 2 only fires when tier 1 is entirely empty AND no segments
  // exist to derive from.
  const coveredSpeakers = new Set(
    contributions.map((c) => c.speaker.toLowerCase()),
  );
  const segmentFallback = deriveContributionsFromSegments(
    segments,
    speakerDisplayNameById,
  );
  const additions = segmentFallback.filter(
    (c) => !coveredSpeakers.has(c.speaker.toLowerCase()),
  );
  let usedContributions = [...contributions, ...additions];
  if (usedContributions.length === 0) {
    usedContributions = deriveContributionsFromChapters(chapters, speakerDisplayNameById);
  }
  const ownerName = overview.owner?.display_name?.toLowerCase() || "";

  return (
    <article className="mm-minutes">
      <div className="mm-minutes-toolbar">
        <div className="mm-minutes-toggle" role="tablist" aria-label="Minutes view">
          <button
            type="button"
            id="mm-minutes-tab-person"
            className={mode === "person" ? "mm-minutes-toggle-btn is-active" : "mm-minutes-toggle-btn"}
            onClick={() => setMode("person")}
            role="tab"
            aria-selected={mode === "person"}
            aria-controls="mm-minutes-panel"
          >
            <span aria-hidden="true">☉</span> By person
          </button>
          <button
            type="button"
            id="mm-minutes-tab-time"
            className={mode === "time" ? "mm-minutes-toggle-btn is-active" : "mm-minutes-toggle-btn"}
            onClick={() => setMode("time")}
            role="tab"
            aria-selected={mode === "time"}
            aria-controls="mm-minutes-panel"
          >
            <span aria-hidden="true">⏱</span> By time
          </button>
        </div>
        <p className="mm-minutes-intro">
          {mode === "person"
            ? "Each participant's contribution distilled into a sentence or two — what they shared, what they asked, what they committed to."
            : "Chronological detailed minutes — chapter headings + the facts and decisions discussed inside each."}
        </p>
      </div>

      <div
        id="mm-minutes-panel"
        role="tabpanel"
        aria-labelledby={mode === "person" ? "mm-minutes-tab-person" : "mm-minutes-tab-time"}
      >
      {mode === "person" ? (
        usedContributions.length === 0 ? (
          <p className="mm-empty">
            No participant contributions extracted yet. Re-extract this meeting
            to populate, or open the Transcript tab.
          </p>
        ) : (
          <ul className="mm-attendee-list">
            {usedContributions.map((entry, index) => {
              const isYou =
                !!ownerName && entry.speaker.toLowerCase().includes(ownerName);
              const speakerNumber = (index % 6) + 1;
              return (
                <li
                  key={`${entry.speaker}-${index}`}
                  className={
                    isYou
                      ? "mm-attendee-row mm-attendee-row-you"
                      : "mm-attendee-row"
                  }
                >
                  <div className="mm-attendee-head">
                    <SpeakerAvatar
                      name={entry.speaker}
                      speakerNumber={speakerNumber}
                      size={32}
                    />
                    <h3 className="mm-attendee-name">
                      {entry.speaker}
                      {isYou && (
                        <span className="mm-pill-yours" style={{ marginLeft: 8 }}>
                          ⌖ you
                        </span>
                      )}
                    </h3>
                  </div>
                  <p className="mm-attendee-contribution">
                    {renderMentions(entry.contribution)}
                  </p>
                </li>
              );
            })}
          </ul>
        )
      ) : chapters.length === 0 ? (
        <p className="mm-empty">
          No chapters detected yet. Re-extract this meeting or switch to
          By-person view.
        </p>
      ) : (
        <div className="mm-time-minutes">
          {chapters.map((chapter) => (
            <section key={chapter.id} className="mm-time-chapter">
              <h3 className="mm-time-chapter-head">
                <span className="mm-ts">[{formatMs(chapter.startMs)}]</span>{" "}
                {chapter.title}
              </h3>
              {chapter.summary && (
                <p className="mm-time-chapter-summary">
                  {renderMentions(chapter.summary)}
                </p>
              )}
              {chapter.bullets.length > 0 && (
                <ul className="mm-time-chapter-bullets">
                  {chapter.bullets.map((bullet, index) => {
                    const speakerName =
                      speakerDisplayNameById.get(bullet.speakerId) || bullet.speakerId;
                    const number = speakerNumberOf(bullet.speakerId);
                    return (
                      <li key={`${chapter.id}-${index}`}>
                        <span className="mm-time-bullet-speaker">
                          <SpeakerChip name={speakerName} speakerNumber={number} />
                        </span>
                        <span>{renderMentions(bullet.text)}</span>
                      </li>
                    );
                  })}
                </ul>
              )}
            </section>
          ))}
        </div>
      )}
      </div>
    </article>
  );
}

function deriveContributionsFromSegments(
  segments: Segment[],
  speakerDisplayNameById: Map<string, string>,
): Array<{ speaker: string; contribution: string; source_segment_ids: number[] }> {
  // v0.2.10 third-tier fallback: when the extract LLM never produced
  // participant_contributions and chapter inference is empty, group
  // raw transcript segments by speaker and pick a representative
  // snippet (the longest segment per speaker). At minimum this
  // guarantees every diarized speaker is visible in the Minutes view
  // so the user can see WHO spoke, even if the system hasn't yet
  // distilled WHAT each contributed.
  const grouped = new Map<
    string,
    { longest: Segment; total: number; ids: number[] }
  >();
  for (const seg of segments) {
    const name =
      speakerDisplayNameById.get(seg.diarization_speaker_id) ||
      seg.diarization_speaker_id;
    const bucket = grouped.get(name);
    if (!bucket) {
      grouped.set(name, { longest: seg, total: 1, ids: [seg.id] });
    } else {
      bucket.total += 1;
      bucket.ids.push(seg.id);
      if (seg.text.length > bucket.longest.text.length) bucket.longest = seg;
    }
  }
  // Sort by descending total-utterance count so the most-frequent
  // speakers appear first.
  return Array.from(grouped.entries())
    .sort(([, a], [, b]) => b.total - a.total)
    .map(([speaker, data]) => ({
      speaker,
      contribution: truncate(data.longest.text, 280),
      source_segment_ids: data.ids,
    }));
}

function deriveContributionsFromChapters(
  chapters: Chapter[],
  speakerDisplayNameById: Map<string, string>,
): Array<{ speaker: string; contribution: string; source_segment_ids: number[] }> {
  // Fallback for meetings extracted before participant_contributions
  // existed. Groups chapter bullets by topSpeakerId and joins the bullet
  // text for each speaker into a single line. Approximate, but better
  // than an empty Minutes view.
  const grouped = new Map<string, { lines: string[]; ids: number[] }>();
  chapters.forEach((chapter) => {
    chapter.bullets.forEach((bullet) => {
      const name = speakerDisplayNameById.get(bullet.speakerId) || bullet.speakerId;
      const bucket = grouped.get(name) ?? { lines: [], ids: [] };
      bucket.lines.push(truncate(bullet.text, 140));
      grouped.set(name, bucket);
    });
  });
  return Array.from(grouped.entries()).map(([speaker, data]) => ({
    speaker,
    contribution: data.lines.slice(0, 3).join(" · "),
    source_segment_ids: data.ids,
  }));
}

// Speaker-name color machinery lives in ./speakerColors so the helpers
// can be reused from components/*.tsx without circular imports. main.tsx
// builds the per-meeting name→slot map and supplies it via the
// provider; renderMentions reads it from context on every call.
import {
  buildSpeakerNameSlots,
  renderMentions,
  SpeakerNameProvider,
  type SpeakerNameSlots,
} from "./speakerColors";

// ── Transcript view (with chapter index + audio scrubber) ─────────────────
function TranscriptView({
  detail,
  query,
  setQuery,
  filteredSegments,
  segmentById,
  candidates,
  keyTerms,
  ownerTerms,
  showConfidenceChips,
  speakerNumberOf,
  speakerDisplayNameById,
  chapters,
  activeSegmentId,
  setActiveSegmentId,
  onBack,
  onCorrectSegment,
  onEditSpeaker,
  onRunAudioAlternatives,
  onAcceptAlternative,
  onAfterSegmentRevert,
}: {
  detail: MeetingDetail;
  query: string;
  setQuery: (value: string) => void;
  filteredSegments: Segment[];
  segmentById: Map<number, Segment>;
  candidates: TranscriptCandidate[];
  keyTerms: string[];
  ownerTerms: string[];
  showConfidenceChips: boolean;
  speakerNumberOf: (id: string) => number;
  speakerDisplayNameById: Map<string, string>;
  chapters: Chapter[];
  activeSegmentId: number | null;
  setActiveSegmentId: (id: number | null) => void;
  onBack: () => void;
  onCorrectSegment: (segment: Segment, text: string) => void;
  onEditSpeaker: (segment: Segment) => void;
  onRunAudioAlternatives: () => Promise<void>;
  onAcceptAlternative: (candidate: TranscriptCandidate) => Promise<void>;
  onAfterSegmentRevert: () => void;
}) {
  const totalDurationSeconds = Math.max(1, detail.meeting.duration_seconds);
  const [confidenceMode, setConfidenceMode] = useState<"all" | "low">("all");
  const scrubberRef = useRef<ScrubberHandle | null>(null);
  const [wavePlayheadSeconds, setWavePlayheadSeconds] = useState(0);

  const visibleSegments = useMemo(
    () =>
      confidenceMode === "low"
        ? filteredSegments.filter((segment) => (segment.confidence ?? 1) < 0.7)
        : filteredSegments,
    [filteredSegments, confidenceMode],
  );

  // v0.2.9: index v0.2.2 linguistic overlap hints by segment_id so each
  // TranscriptRow can find its own hint in O(1) instead of every row
  // scanning the array. Built once per detail change.
  // Backend orders by `segment_id, confidence DESC, kind`, so the first
  // hint we see for a given segment is the highest-confidence one. Skip
  // subsequent hits to keep that one rather than overwriting.
  const overlapHintsBySegmentId = useMemo(() => {
    const map = new Map<number, OverlapHint>();
    for (const hint of detail.overlap_hints ?? []) {
      if (!map.has(hint.segment_id)) {
        map.set(hint.segment_id, hint);
      }
    }
    return map;
  }, [detail.overlap_hints]);

  return (
    <>
      <div className="mm-review-hero">
        <div style={{ flex: 1, minWidth: 0 }}>
          <div className="mm-lbl-strong">Transcript</div>
          <h1>
            The <Ital>transcript</Ital>
          </h1>
          <div style={{ fontSize: 13, color: "var(--mm-ink-3)", marginTop: 8 }}>
            {detail.meeting.title}
          </div>
        </div>
        <div className="mm-review-actions" style={{ gridTemplateColumns: "auto" }}>
          <button type="button" className="mm-btn" onClick={onBack}>
            ↩ Back to review
          </button>
        </div>
      </div>
      <hr className="mm-rule" style={{ margin: "22px 0" }} />

      <ScrubberBar
        ref={scrubberRef}
        meetingId={detail.meeting.id}
        totalSeconds={totalDurationSeconds}
        chapters={chapters}
        onTime={(seconds) => setWavePlayheadSeconds(seconds)}
      />

      <Waveform
        meetingId={detail.meeting.id}
        totalSeconds={totalDurationSeconds}
        playheadSeconds={wavePlayheadSeconds}
        onSeek={(seconds) => {
          setWavePlayheadSeconds(seconds);
          scrubberRef.current?.seek(seconds);
        }}
      />

      <div className="mm-row-wrap" style={{ marginBottom: 22 }}>
        <input
          className="mm-input"
          style={{ maxWidth: 380 }}
          placeholder="⌕  Search transcript"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
        />
        <span className="mm-lbl">Scope</span>
        <Pill
          tone={confidenceMode === "all" ? "clay" : "quiet"}
          onClick={() => setConfidenceMode("all")}
          active={confidenceMode === "all"}
        >
          all
        </Pill>
        <Pill
          tone={confidenceMode === "low" ? "clay" : "quiet"}
          onClick={() => setConfidenceMode("low")}
          active={confidenceMode === "low"}
        >
          low confidence
        </Pill>
      </div>

      <AudioAlternativesPanel
        meetingId={detail.meeting.id}
        candidates={candidates}
        segmentById={segmentById}
        onRun={onRunAudioAlternatives}
        onAccept={onAcceptAlternative}
      />

      <div className="mm-transcript-layout">
        <div>
          {visibleSegments.map((segment) => {
            const num = speakerNumberOf(segment.diarization_speaker_id);
            const name =
              speakerDisplayNameById.get(segment.diarization_speaker_id) ||
              segment.diarization_speaker_id;
            return (
              <TranscriptRow
                key={segment.id}
                meetingId={detail.meeting.id}
                segment={segment}
                speakerName={name}
                speakerNumber={num}
                highlightTerms={keyTerms}
                ownerTerms={ownerTerms}
                showConfidenceChips={showConfidenceChips}
                active={segment.id === activeSegmentId}
                overlapHint={overlapHintsBySegmentId.get(segment.id)}
                onCorrectSegment={onCorrectSegment}
                onEditSegmentSpeaker={onEditSpeaker}
                onAfterRevert={onAfterSegmentRevert}
              />
            );
          })}
          {!visibleSegments.length && (
            <p className="mm-empty">
              {confidenceMode === "low"
                ? "No low-confidence segments — nothing needs review."
                : "No transcript segments yet."}
            </p>
          )}
        </div>

        <aside className="mm-chapter-rail">
          <div className="mm-lbl-strong">Chapters</div>
          {chapters.length ? (
            <ul className="mm-chapter-list">
              {chapters.map((chapter) => {
                const isActive =
                  activeSegmentId !== null && chapter.segmentIds.includes(activeSegmentId);
                return (
                  <li
                    key={chapter.id}
                    className={isActive ? "is-active" : undefined}
                    onClick={() => {
                      const firstId = chapter.segmentIds[0];
                      if (firstId) setActiveSegmentId(firstId);
                      scrubberRef.current?.seek(chapter.startMs / 1000);
                    }}
                  >
                    <span className="mm-ts">{formatMs(chapter.startMs)}</span>
                    <span>{chapter.title}</span>
                  </li>
                );
              })}
            </ul>
          ) : (
            <p className="mm-empty" style={{ fontSize: 12 }}>
              No chapters yet.
            </p>
          )}
        </aside>
      </div>
    </>
  );
}

type ScrubberHandle = {
  seek: (seconds: number) => void;
};

const ScrubberBar = React.forwardRef<
  ScrubberHandle,
  {
    meetingId: number;
    totalSeconds: number;
    chapters: Chapter[];
    onTime?: (seconds: number, playing: boolean) => void;
  }
>(function ScrubberBar({ meetingId, totalSeconds, chapters, onTime }, ref) {
  const audioRef = useRef<HTMLAudioElement | null>(null);
  // Track the handlers we attach so we can detach them when the meeting
  // changes — otherwise the prior audio element holds GC-rooted listeners
  // pointing at stale state setters.
  const handlersRef = useRef<{
    timeupdate: () => void;
    ended: () => void;
    pause: () => void;
    play: () => void;
  } | null>(null);
  const [playing, setPlaying] = useState(false);
  const [currentSeconds, setCurrentSeconds] = useState(0);
  // Playback rate is wired to the audio element via setPlaybackRate so the
  // dropdown actually changes playback speed. State persists across
  // play/pause without re-creating the Audio node.
  const [playbackRate, setPlaybackRate] = useState(1);
  const onTimeRef = useRef(onTime);
  useEffect(() => {
    onTimeRef.current = onTime;
  }, [onTime]);
  useEffect(() => {
    if (audioRef.current) audioRef.current.playbackRate = playbackRate;
  }, [playbackRate]);

  // Tear down any audio + listeners when the meeting changes or on unmount.
  useEffect(() => {
    return () => {
      const audio = audioRef.current;
      const handlers = handlersRef.current;
      if (audio && handlers) {
        audio.removeEventListener("timeupdate", handlers.timeupdate);
        audio.removeEventListener("ended", handlers.ended);
        audio.removeEventListener("pause", handlers.pause);
        audio.removeEventListener("play", handlers.play);
      }
      audio?.pause();
      audioRef.current = null;
      handlersRef.current = null;
    };
  }, [meetingId]);

  const ensureAudio = () => {
    if (!audioRef.current) {
      // Pause any other audio (e.g. a per-segment clip) before claiming the
      // shared activeAudio slot.
      pauseActiveAudio();
      const audio = new Audio(`/api/meetings/${meetingId}/audio`);
      audio.playbackRate = playbackRate;
      const handlers = {
        timeupdate: () => {
          setCurrentSeconds(audio.currentTime);
          onTimeRef.current?.(audio.currentTime, !audio.paused);
        },
        ended: () => {
          setPlaying(false);
          onTimeRef.current?.(audio.currentTime, false);
        },
        pause: () => {
          setPlaying(false);
          onTimeRef.current?.(audio.currentTime, false);
        },
        play: () => {
          setPlaying(true);
          onTimeRef.current?.(audio.currentTime, true);
        },
      };
      audio.addEventListener("timeupdate", handlers.timeupdate);
      audio.addEventListener("ended", handlers.ended);
      audio.addEventListener("pause", handlers.pause);
      audio.addEventListener("play", handlers.play);
      audioRef.current = audio;
      handlersRef.current = handlers;
      setActiveAudio(audio);
    } else if (getActiveAudio() !== audioRef.current) {
      pauseActiveAudio();
      setActiveAudio(audioRef.current);
    }
    return audioRef.current;
  };

  React.useImperativeHandle(
    ref,
    () => ({
      seek(seconds: number) {
        const audio = ensureAudio();
        const clamped = Math.max(0, Math.min(totalSeconds, seconds));
        const finish = () => {
          audio.currentTime = clamped;
          void audio.play().catch(() => undefined);
        };
        if (audio.readyState >= 1) {
          finish();
        } else {
          audio.addEventListener("loadedmetadata", finish, { once: true });
          audio.load();
        }
      },
    }),
    // ensureAudio captures meetingId; recreate when source changes
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [meetingId, totalSeconds],
  );

  const togglePlay = () => {
    const audio = ensureAudio();
    if (playing) audio.pause();
    else void audio.play().catch(() => undefined);
  };

  const seekTo = (event: React.MouseEvent<HTMLDivElement>) => {
    const audio = ensureAudio();
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    audio.currentTime = Math.max(0, Math.min(totalSeconds, totalSeconds * ratio));
  };

  const skip = (delta: number) => {
    const audio = ensureAudio();
    audio.currentTime = Math.max(0, Math.min(totalSeconds, audio.currentTime + delta));
  };

  const progress = Math.min(100, (currentSeconds / totalSeconds) * 100);

  return (
    <div className="mm-scrub">
      <span className="mm-scrub-time">
        {formatSeconds(currentSeconds)} / {formatSeconds(totalSeconds)}
      </span>
      <div
        className="mm-scrub-bar"
        onClick={seekTo}
        role="slider"
        aria-label="Audio progress"
        aria-valuemin={0}
        aria-valuemax={Math.round(totalSeconds)}
        aria-valuenow={Math.round(currentSeconds)}
        aria-valuetext={`${formatSeconds(currentSeconds)} of ${formatSeconds(totalSeconds)}`}
        tabIndex={0}
      >
        <div className="mm-scrub-bar-fill" style={{ width: `${progress}%` }} />
        {chapters.map((chapter) => {
          const ratio = Math.max(0, Math.min(1, chapter.startMs / 1000 / totalSeconds));
          if (ratio <= 0) return null;
          return (
            <span
              key={`tick-${chapter.id}`}
              className="mm-scrub-tick"
              style={{ left: `${ratio * 100}%` }}
              title={`${formatMs(chapter.startMs)} · ${chapter.title}`}
            />
          );
        })}
      </div>
      <div className="mm-scrub-controls">
        <button type="button" className="mm-icon-btn" onClick={() => skip(-15)} aria-label="Rewind 15 seconds">
          ⤺
        </button>
        <button type="button" className="mm-icon-btn" onClick={togglePlay} aria-label={playing ? "Pause" : "Play"}>
          {playing ? "❚❚" : "▷"}
        </button>
        <button type="button" className="mm-icon-btn" onClick={() => skip(15)} aria-label="Skip 15 seconds">
          ⤻
        </button>
      </div>
      <select
        className="mm-scrub-rate"
        value={playbackRate}
        onChange={(event) => setPlaybackRate(Number(event.target.value))}
        title="Playback speed"
        aria-label="Playback speed"
      >
        {[0.75, 1, 1.25, 1.5, 2].map((rate) => (
          <option key={rate} value={rate}>
            {rate}×
          </option>
        ))}
      </select>
    </div>
  );
});



// ── Audio alternatives ────────────────────────────────────────────────────
function AudioAlternativesPanel({
  meetingId,
  candidates,
  segmentById,
  onRun,
  onAccept,
}: {
  meetingId: number | null;
  candidates: TranscriptCandidate[];
  segmentById: Map<number, Segment>;
  onRun: () => Promise<void>;
  onAccept: (candidate: TranscriptCandidate) => Promise<void>;
}) {
  const visible = candidates.filter(
    (candidate) => !["stale", "rejected", "superseded"].includes(candidate.status),
  );
  const suggestedCount = visible.filter((candidate) => candidate.status === "suggested").length;

  return (
    <details className="mm-alt-panel">
      <summary>
        <span>⚯ Advanced audio repair log</span>
        <small className="mm-lbl">
          {suggestedCount ? `${suggestedCount} saved` : "automatic"}
        </small>
      </summary>
      <div className="mm-alt-body">
        <div>
          <strong>Consensus repair</strong>
          <p>
            MeetingMind runs multiple ASR profiles on suspicious spans and auto-applies only
            high-agreement repairs. This log is for audit or manual rerun.
          </p>
        </div>
        <button
          type="button"
          className="mm-btn"
          disabled={!meetingId}
          onClick={() => void onRun()}
        >
          ↻ Rerun repair pass
        </button>
      </div>
      <div>
        {visible.map((candidate) => (
          <CandidateCard
            key={candidate.id}
            meetingId={meetingId}
            candidate={candidate}
            currentSegment={segmentById.get(candidate.segment_id)}
            onAccept={() => onAccept(candidate)}
          />
        ))}
        {!visible.length && (
          <p className="mm-empty" style={{ marginTop: 12, fontSize: 13 }}>
            No audio alternatives generated for this meeting.
          </p>
        )}
      </div>
    </details>
  );
}

function CandidateCard({
  meetingId,
  candidate,
  currentSegment,
  onAccept,
}: {
  meetingId: number | null;
  candidate: TranscriptCandidate;
  currentSegment?: Segment;
  onAccept: () => Promise<void>;
}) {
  const metrics = parseCandidateMetrics(candidate.metrics_json);
  const changes = candidateChangeSnippets(currentSegment?.text || "", candidate.text);
  return (
    <article
      className={candidate.status === "accepted" ? "mm-candidate-card is-accepted" : "mm-candidate-card"}
    >
      <header>
        <div>
          <strong>{formatMs(candidate.start_ms)} audio alternative</strong>
          <div className="mm-lbl">
            {candidate.profile_name} · {candidate.status}
          </div>
        </div>
        <div className="mm-row">
          <CandidatePlaybackButton
            meetingId={meetingId}
            startMs={candidate.start_ms}
            endMs={candidate.end_ms}
          />
          {candidate.status === "suggested" && (
            <button
              type="button"
              className="mm-btn mm-btn-primary mm-btn-sm"
              onClick={() => void onAccept()}
            >
              ✓ Accept
            </button>
          )}
        </div>
      </header>
      <div className="mm-candidate-change">
        {changes.length ? changes : <p className="mm-empty">No material text change.</p>}
      </div>
      <details>
        <summary className="mm-lbl">show full replacement text</summary>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12, marginTop: 8 }}>
          <div>
            <span className="mm-lbl">Current</span>
            <p style={{ margin: "4px 0 0", fontSize: 13 }}>{currentSegment?.text || "—"}</p>
          </div>
          <div>
            <span className="mm-lbl">Alternative</span>
            <p style={{ margin: "4px 0 0", fontSize: 13 }}>{candidate.text}</p>
          </div>
        </div>
      </details>
      <div className="mm-candidate-metrics">
        <span>score {formatConfidence(candidate.score)}</span>
        {Object.entries(metrics).map(([key, value]) => (
          <span key={`${candidate.id}-${key}`}>
            {formatMetricLabel(key)} {String(value)}
          </span>
        ))}
      </div>
    </article>
  );
}

function CandidatePlaybackButton({
  meetingId,
  startMs,
  endMs,
}: {
  meetingId: number | null;
  startMs: number;
  endMs: number;
}) {
  const [playing, setPlaying] = useState(false);
  const audioRef = useRef<HTMLAudioElement | null>(null);
  useEffect(() => () => audioRef.current?.pause(), []);

  const toggle = () => {
    if (!meetingId) return;
    if (playing) {
      audioRef.current?.pause();
      setPlaying(false);
      return;
    }
    audioRef.current = playExactAudioSpan(
      meetingId,
      startMs,
      endMs,
      () => setPlaying(false),
      () => undefined,
      () => setPlaying(false),
    );
    setPlaying(true);
  };

  return (
    <button
      type="button"
      className="mm-icon-btn"
      onClick={toggle}
      disabled={!meetingId}
      aria-label={playing ? "Pause" : "Play"}
    >
      {playing ? "❚❚" : "▷"}
    </button>
  );
}

// ── Speaker edit modal ────────────────────────────────────────────────────
function SpeakerEditModal({
  segment,
  speakerIds,
  speakerAliasById,
  speakerDisplayNameById,
  speakerNumberOf,
  suggestions,
  onRename,
  onMoveCard,
  onMoveAll,
  onClose,
}: {
  segment: Segment;
  speakerIds: string[];
  speakerAliasById: Map<string, string>;
  speakerDisplayNameById: Map<string, string>;
  speakerNumberOf: (id: string) => number;
  suggestions: SpeakerSuggestion[];
  onRename: (speakerId: string, label: string) => Promise<void>;
  onMoveCard: (targetSpeakerId: string) => Promise<void>;
  onMoveAll: (targetSpeakerId: string) => Promise<void>;
  onClose: () => void;
}) {
  const currentSpeakerId = segment.diarization_speaker_id;
  const currentAlias = speakerAliasById.get(currentSpeakerId) || currentSpeakerId;
  const currentName = speakerDisplayNameById.get(currentSpeakerId) || currentAlias;
  const currentNumber = speakerNumberOf(currentSpeakerId);
  const [draftName, setDraftName] = useState(currentName);
  const nameInputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => setDraftName(currentName), [currentName]);
  useEffect(() => {
    nameInputRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  return (
    <div className="mm-modal-backdrop" role="presentation" onClick={onClose}>
      <section
        className="mm-modal"
        role="dialog"
        aria-modal="true"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="mm-modal-head">
          <SpeakerChip name={currentAlias} speakerNumber={currentNumber} />
          <button type="button" className="mm-btn mm-btn-ghost" onClick={onClose}>
            Close ✕
          </button>
        </header>
        <div className="mm-modal-body">
          <h2>
            Change <Ital>speaker</Ital>
          </h2>
          <div className="mm-modal-sub">
            at {formatMs(segment.start_ms)} · edits apply only where you choose
          </div>
        </div>

        <div className="mm-modal-section">
          <h3>Rename in this meeting</h3>
          <div className="mm-modal-name-row">
            <input
              ref={nameInputRef}
              className="mm-input mm-input-square"
              value={draftName}
              onChange={(event) => setDraftName(event.target.value)}
              aria-label="Speaker name"
            />
            <button
              type="button"
              className="mm-btn mm-btn-primary"
              onClick={() => void onRename(currentSpeakerId, draftName)}
            >
              ✓ Save name
            </button>
          </div>
          {!!suggestions.length && (
            <div className="mm-modal-suggestion">
              {suggestions.slice(0, 2).map((suggestion) => (
                <div key={suggestion.item.id} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
                  <div className="mm-suggestion-row">
                    <strong>{suggestion.candidateName}</strong>
                    <Pill tone="clay">{Math.round(suggestion.confidence * 100)}% match</Pill>
                  </div>
                  <p>{suggestion.basis}</p>
                  {!!suggestion.evidence.length && (
                    <div className="mm-suggestion-evidence">
                      {suggestion.evidence.slice(0, 3).map((evidence, index) => (
                        <span key={`${suggestion.item.id}-${index}`}>{evidence}</span>
                      ))}
                    </div>
                  )}
                  <div className="mm-modal-suggestion-actions">
                    <span>segments {suggestion.sourceSegmentIds.join(", ") || "n/a"}</span>
                    <button
                      type="button"
                      className="mm-btn mm-btn-sm"
                      onClick={() => setDraftName(suggestion.candidateName)}
                    >
                      Use name
                    </button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="mm-modal-section">
          <h3>Move card to another speaker</h3>
          <div className="mm-modal-choices">
            {speakerIds
              .filter((speakerId) => speakerId !== currentSpeakerId)
              .map((speakerId) => {
                const alias = speakerAliasById.get(speakerId) || speakerId;
                const name = speakerDisplayNameById.get(speakerId) || alias;
                const num = speakerNumberOf(speakerId);
                return (
                  <button
                    key={speakerId}
                    type="button"
                    className="mm-modal-choice"
                    onClick={() => void onMoveCard(speakerId)}
                  >
                    <SpeakerChip name={alias} speakerNumber={num} />
                    <span className="mm-choice-name">{name}</span>
                  </button>
                );
              })}
          </div>
        </div>

        <div className="mm-modal-foot">
          <details>
            <summary>▸ Move every {currentName} card</summary>
            <div className="mm-modal-choices" style={{ marginTop: 10 }}>
              {speakerIds
                .filter((speakerId) => speakerId !== currentSpeakerId)
                .map((speakerId) => {
                  const alias = speakerAliasById.get(speakerId) || speakerId;
                  const name = speakerDisplayNameById.get(speakerId) || alias;
                  const num = speakerNumberOf(speakerId);
                  return (
                    <button
                      key={speakerId}
                      type="button"
                      className="mm-modal-choice"
                      onClick={() => void onMoveAll(speakerId)}
                    >
                      <SpeakerChip name={alias} speakerNumber={num} />
                      <span className="mm-choice-name">{name}</span>
                    </button>
                  );
                })}
            </div>
          </details>
          <span>
            <span className="mm-kbd">↵</span> save · <span className="mm-kbd">esc</span> close
          </span>
        </div>
      </section>
    </div>
  );
}

// ── Workstreams screen ────────────────────────────────────────────────────
function WorkstreamsScreen({
  workstreams,
  crossMeetingQuery,
  setCrossMeetingQuery,
  searchResults,
  onSearch,
  onOpenResult,
  onSelectMeeting,
  onDelete,
  onRename,
}: {
  workstreams: WorkstreamIntelligence[];
  crossMeetingQuery: string;
  setCrossMeetingQuery: (value: string) => void;
  searchResults: SearchResult[];
  onSearch: () => Promise<void>;
  onOpenResult: (result: SearchResult) => void;
  onSelectMeeting: (id: number) => void;
  onDelete: (name: string) => void;
  onRename: (oldName: string, newName: string) => Promise<void>;
}) {
  const [sort, setSort] = useState<"confidence" | "mentions" | "recency">("confidence");
  const [filter, setFilter] = useState<"all" | "established" | "emerging">("all");
  const [minConfidence, setMinConfidence] = useState<number>(() => {
    const stored = Number(localStorage.getItem("mm-ws-min-conf") || "0");
    return Number.isFinite(stored) ? stored : 0;
  });
  useEffect(() => {
    localStorage.setItem("mm-ws-min-conf", String(minConfidence));
  }, [minConfidence]);
  const [renamingName, setRenamingName] = useState<string | null>(null);
  const [renameDraft, setRenameDraft] = useState("");

  const filtered = useMemo(() => {
    return workstreams.filter((stream) => {
      if (stream.avg_confidence * 100 < minConfidence) return false;
      if (filter === "established" && stream.meeting_count < 3) return false;
      if (filter === "emerging" && stream.meeting_count >= 3) return false;
      return true;
    });
  }, [workstreams, filter, minConfidence]);
  const sorted = useMemo(() => {
    const copy = [...filtered];
    if (sort === "confidence") copy.sort((a, b) => b.avg_confidence - a.avg_confidence);
    if (sort === "mentions") copy.sort((a, b) => b.mention_count - a.mention_count);
    if (sort === "recency") copy.sort((a, b) => b.meeting_count - a.meeting_count);
    return copy;
  }, [filtered, sort]);
  const highCount = workstreams.filter((stream) => stream.avg_confidence >= 0.8).length;
  const establishedCount = workstreams.filter((stream) => stream.meeting_count >= 3).length;
  const emergingCount = workstreams.length - establishedCount;

  return (
    <section className="mm-screen">
      <div className="mm-screen-header">
        <div>
          <div className="mm-lbl-strong">03 · threads</div>
          <h1>
            The <Ital>workstreams</Ital>
          </h1>
          <p className="mm-screen-sub">
            Threads MeetingMind has noticed across your recordings. Review them before they become vault links.
          </p>
        </div>
      </div>
      <hr className="mm-rule" style={{ margin: "0 0 26px" }} />

      <div className="mm-ws-toolbar">
        <input
          className="mm-input"
          placeholder="⌕  Search across transcripts and review items"
          value={crossMeetingQuery}
          onChange={(event) => setCrossMeetingQuery(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter") void onSearch();
          }}
        />
        <button type="button" className="mm-btn" onClick={() => void onSearch()}>
          ⌕ Search
        </button>
        <div className="mm-ws-sort">
          <span className="mm-lbl">Sort</span>
          {(["confidence", "mentions", "recency"] as const).map((option) => (
            <Pill
              key={option}
              tone="quiet"
              active={sort === option}
              onClick={() => setSort(option)}
            >
              {option}
            </Pill>
          ))}
        </div>
      </div>

      <div className="mm-ws-toolbar" style={{ marginTop: -8 }}>
        <span className="mm-lbl">Filter</span>
        {(["all", "established", "emerging"] as const).map((option) => (
          <Pill key={option} tone="quiet" active={filter === option} onClick={() => setFilter(option)}>
            {option === "all" ? `all (${workstreams.length})` : option === "established" ? `established (${establishedCount})` : `emerging (${emergingCount})`}
          </Pill>
        ))}
        <span className="mm-lbl" style={{ marginLeft: 18 }}>
          Min confidence
        </span>
        <input
          type="range"
          min={0}
          max={100}
          step={5}
          value={minConfidence}
          onChange={(event) => setMinConfidence(Number(event.target.value))}
          style={{ width: 140, accentColor: "var(--mm-clay)" }}
          aria-label="Minimum confidence threshold"
        />
        <span className="mm-mono" style={{ fontSize: 11, color: "var(--mm-ink-3)", width: 36 }}>
          {minConfidence}%
        </span>
      </div>

      {!!searchResults.length && (
        <div className="mm-search-results">
          {searchResults.map((result) => (
            <button
              key={`${result.result_type}-${result.meeting_id}-${result.segment_id || result.review_item_id}`}
              type="button"
              className="mm-search-result"
              onClick={() => onOpenResult(result)}
            >
              <span className="mm-search-kind">
                {result.result_type === "segment"
                  ? "Transcript"
                  : result.result_type === "review_item"
                    ? "Workstream"
                    : result.result_type}
              </span>
              <strong>{result.meeting_title}</strong>
              <p>
                {result.start_ms !== null && result.start_ms !== undefined
                  ? `${formatMs(result.start_ms)} · `
                  : ""}
                <strong style={{ color: "var(--mm-clay)" }}>{result.speaker}</strong>: {result.text}
              </p>
              {result.context_text && result.context_text !== result.text && (
                <p style={{ color: "var(--mm-ink-3)" }}>{result.context_text}</p>
              )}
            </button>
          ))}
        </div>
      )}

      <div className="mm-ws-panel">
        <div className="mm-ws-panel-head">
          <div>
            <div className="mm-lbl-strong">Workstream intelligence</div>
            <div className="mm-lbl" style={{ marginTop: 4 }}>
              {workstreams.length} active
            </div>
          </div>
          <div className="mm-row" style={{ flexWrap: "wrap" }}>
            <Pill tone="clay">{highCount} high</Pill>
            <Pill tone="sage">{establishedCount} established</Pill>
            <Pill tone="quiet">{emergingCount} emerging</Pill>
          </div>
        </div>
        {sorted.map((stream) => {
          const conf = Math.round(stream.avg_confidence * 100);
          const isEstablished = stream.meeting_count >= 3;
          const isRenaming = renamingName === stream.display_name;
          return (
            <div key={stream.display_name} className="mm-ws-row">
              <div
                className="mm-ws-ring"
                style={{
                  background: `conic-gradient(var(--mm-clay) 0 ${stream.avg_confidence * 360}deg, var(--mm-bone-3) ${stream.avg_confidence * 360}deg 360deg)`,
                }}
              >
                <div>{conf}</div>
              </div>
              <div>
                {isRenaming ? (
                  <div className="mm-row" style={{ gap: 6 }}>
                    <input
                      className="mm-input mm-input-square"
                      style={{ width: 320, fontSize: 14 }}
                      autoFocus
                      value={renameDraft}
                      onChange={(event) => setRenameDraft(event.target.value)}
                      onKeyDown={(event) => {
                        if (event.key === "Enter") {
                          void onRename(stream.display_name, renameDraft);
                          setRenamingName(null);
                        } else if (event.key === "Escape") {
                          setRenamingName(null);
                        }
                      }}
                    />
                    <button
                      type="button"
                      className="mm-btn mm-btn-primary mm-btn-sm"
                      onClick={() => {
                        void onRename(stream.display_name, renameDraft);
                        setRenamingName(null);
                      }}
                    >
                      ✓ save
                    </button>
                    <button
                      type="button"
                      className="mm-btn mm-btn-ghost mm-btn-sm"
                      onClick={() => setRenamingName(null)}
                    >
                      cancel
                    </button>
                  </div>
                ) : (
                  <div className="mm-ws-name">{stream.display_name}</div>
                )}
                <div className="mm-row" style={{ gap: 6, marginTop: 6, flexWrap: "wrap" }}>
                  <Pill tone={isEstablished ? "sage" : "quiet"}>
                    {isEstablished ? "established" : "emerging"}
                  </Pill>
                  <span className="mm-lbl" style={{ fontSize: 9 }}>
                    {stream.meeting_count} meeting{stream.meeting_count === 1 ? "" : "s"} ·{" "}
                    {stream.mention_count} mention{stream.mention_count === 1 ? "" : "s"}
                  </span>
                </div>
              </div>
              <div className="mm-ws-meetings">
                {stream.meetings.slice(0, 3).map((meeting) => (
                  <button
                    key={`${stream.display_name}-${meeting.meeting_id}`}
                    type="button"
                    onClick={() => onSelectMeeting(meeting.meeting_id)}
                  >
                    {meeting.meeting_title}
                  </button>
                ))}
                {stream.meetings.length > 3 && (
                  <span className="mm-lbl" style={{ alignSelf: "center" }}>
                    +{stream.meetings.length - 3}
                  </span>
                )}
              </div>
              <div className="mm-row" style={{ gap: 4 }}>
                <button
                  type="button"
                  className="mm-btn mm-btn-ghost mm-btn-sm"
                  onClick={() => {
                    setRenamingName(stream.display_name);
                    setRenameDraft(stream.display_name);
                  }}
                  title="Rename workstream across all meetings"
                >
                  ✎
                </button>
                <button
                  type="button"
                  className="mm-btn mm-btn-danger mm-btn-sm"
                  onClick={() => void onDelete(stream.display_name)}
                  title="Delete workstream and re-promote affected meetings"
                >
                  ✕
                </button>
              </div>
            </div>
          );
        })}
        {!workstreams.length && (
          <p className="mm-empty mm-empty-center">Run extraction to populate workstream history.</p>
        )}
        {workstreams.length > 0 && !sorted.length && (
          <p className="mm-empty mm-empty-center">
            No workstreams match the current filter. Lower the confidence threshold or switch to "all".
          </p>
        )}
      </div>
    </section>
  );
}

// ── People screen ─────────────────────────────────────────────────────────
function PeopleScreen({
  people,
  selectedPersonId,
  personDetail,
  onSelectPerson,
  onClearPerson,
  onOpenMeeting,
  owner,
  onSetOwner,
  onClearOwner,
  onDeletePerson,
  onRenamePerson,
  onPruneOrphans,
}: {
  people: PersonSummary[];
  selectedPersonId: number | null;
  personDetail: PersonDetail | null;
  onSelectPerson: (id: number) => void;
  onClearPerson: () => void;
  onOpenMeeting: (meetingId: number) => void;
  owner: OwnerIdentity;
  onSetOwner: (personId: number | null, displayName: string | null, aliases: string[]) => Promise<void>;
  onClearOwner: () => Promise<void>;
  onDeletePerson: (person: PersonDetail) => void;
  // v0.2.10: cascade-rename across all meetings + merge if target name exists.
  onRenamePerson: (person: PersonDetail, newName: string) => Promise<void>;
  onPruneOrphans: () => void;
}) {
  // v0.2.10: track which person's rename input is open and the draft
  // value. Local to the screen so closing the detail view discards the
  // in-progress edit cleanly.
  const [renameDraft, setRenameDraft] = useState<string | null>(null);
  const orphanCount = people.filter((p) => p.meeting_count === 0 && p.action_count === 0).length;
  const sortedPeople = useMemo(() => {
    return [...people].sort((a, b) => {
      const youDelta = Number(!!b.is_you) - Number(!!a.is_you);
      if (youDelta !== 0) return youDelta;
      const meetings = b.meeting_count - a.meeting_count;
      if (meetings !== 0) return meetings;
      return a.display_name.localeCompare(b.display_name);
    });
  }, [people]);
  return (
    <section className="mm-screen">
      <div className="mm-screen-header">
        <div>
          <div className="mm-lbl-strong">04 · contributors</div>
          <h1>
            The <Ital>people</Ital>
          </h1>
          <p className="mm-screen-sub">
            Everyone you've named on a meeting. Click in to see every recording they joined and every action they own.
          </p>
        </div>
      </div>
      <hr className="mm-rule" style={{ margin: "0 0 26px" }} />

      {selectedPersonId && personDetail ? (
        <div>
          <div className="mm-row" style={{ justifyContent: "space-between", alignItems: "center" }}>
            <button type="button" className="mm-btn mm-btn-ghost mm-btn-sm" onClick={onClearPerson}>
              ← back to all
            </button>
            <div className="mm-row" style={{ gap: 8 }}>
              {/* v0.2.10: rename button cascades the new label across every
                  meeting referencing this person_id (and merges into an
                  existing person row if the target name already exists). */}
              <button
                type="button"
                className="mm-btn mm-btn-sm"
                onClick={() => setRenameDraft(personDetail.display_name)}
                title="Rename across every meeting that references this person"
              >
                ✎ Rename
              </button>
              <button
                type="button"
                className="mm-btn mm-btn-danger mm-btn-sm"
                onClick={() => onDeletePerson(personDetail)}
                title="Remove this person from the directory (transcript text + meetings are untouched)"
              >
                ✕ Remove from People
              </button>
            </div>
          </div>
          <div className="mm-row" style={{ gap: 16, marginTop: 14 }}>
            <SpeakerAvatar
              name={personDetail.display_name}
              speakerNumber={(personDetail.id % 6) + 1}
              size={64}
            />
            <div style={{ flex: 1, minWidth: 0 }}>
              {renameDraft !== null ? (
                <form
                  className="mm-row"
                  style={{ gap: 8, alignItems: "center" }}
                  onSubmit={async (event) => {
                    event.preventDefault();
                    const next = renameDraft.trim();
                    setRenameDraft(null);
                    if (next && next !== personDetail.display_name) {
                      await onRenamePerson(personDetail, next);
                    }
                  }}
                >
                  <input
                    autoFocus
                    className="mm-input mm-input-square"
                    style={{ fontSize: 24, padding: "6px 12px" }}
                    value={renameDraft}
                    onChange={(event) => setRenameDraft(event.target.value)}
                    aria-label="New name"
                    // v0.2.10 audit L4: pair with backend H1 cap.
                    maxLength={200}
                    onKeyDown={(event) => {
                      if (event.key === "Escape") setRenameDraft(null);
                    }}
                  />
                  <button type="submit" className="mm-btn mm-btn-primary mm-btn-sm">
                    ✓ Save
                  </button>
                  <button
                    type="button"
                    className="mm-btn mm-btn-ghost mm-btn-sm"
                    onClick={() => setRenameDraft(null)}
                  >
                    Cancel
                  </button>
                </form>
              ) : (
                <div className="mm-display" style={{ fontSize: 36, lineHeight: 1.05 }}>
                  {personDetail.display_name}
                </div>
              )}
              {personDetail.role && (
                <div className="mm-lbl" style={{ marginTop: 6 }}>
                  {personDetail.role}
                </div>
              )}
              <div className="mm-row" style={{ gap: 8, marginTop: 12 }}>
                <Pill tone="sage">{personDetail.meetings.length} meeting{personDetail.meetings.length === 1 ? "" : "s"}</Pill>
                <Pill tone="clay">{personDetail.actions.length} action{personDetail.actions.length === 1 ? "" : "s"}</Pill>
                {personDetail.aliases.length > 0 && (
                  <Pill tone="quiet">aka {personDetail.aliases.join(", ")}</Pill>
                )}
              </div>
            </div>
          </div>

          <SectionRule label="Meetings" count={personDetail.meetings.length} />
          {personDetail.meetings.length ? (
            <div className="mm-people-list">
              {personDetail.meetings.map((meeting) => (
                <button
                  key={meeting.id}
                  type="button"
                  className="mm-search-result"
                  onClick={() => onOpenMeeting(meeting.id)}
                >
                  <span className="mm-search-kind">{formatDateTime(meeting.created_at)}</span>
                  <strong>{meeting.title}</strong>
                  <p>
                    {Math.max(1, Math.round(meeting.duration_seconds / 60))} min · {meeting.status}
                  </p>
                </button>
              ))}
            </div>
          ) : (
            <p className="mm-empty">No confirmed meetings yet.</p>
          )}

          <SectionRule label="Actions" count={personDetail.actions.length} />
          {personDetail.actions.length ? (
            <ul className="mm-people-actions">
              {personDetail.actions.map((action) => (
                <li key={action.id}>
                  <button
                    type="button"
                    onClick={() => onOpenMeeting(action.meeting_id)}
                    className="mm-mono"
                    style={{
                      background: "transparent",
                      border: "none",
                      color: "var(--mm-clay)",
                      cursor: "pointer",
                      padding: 0,
                      fontSize: 12,
                    }}
                  >
                    [{action.meeting_title}]
                  </button>
                  <span> {action.text}</span>
                  {action.due_date && (
                    <span className="mm-lbl" style={{ marginLeft: 8 }}>
                      due {action.due_date}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mm-empty">No open actions assigned.</p>
          )}
        </div>
      ) : (
        <>
          <div className="mm-owner-card">
            <div className="mm-lbl-strong">Who's running this install?</div>
            {owner.configured ? (
              <div className="mm-row" style={{ gap: 12, marginTop: 10, flexWrap: "wrap" }}>
                <SpeakerAvatar
                  name={owner.display_name || "?"}
                  speakerNumber={((owner.person_id || 1) % 6) + 1}
                  size={36}
                />
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="mm-display" style={{ fontSize: 20 }}>
                    You are <Ital>{owner.display_name}</Ital>
                  </div>
                  <div className="mm-lbl" style={{ marginTop: 4 }}>
                    Your action items and mentions get priority everywhere.
                  </div>
                  {owner.aliases.length > 0 && (
                    <div className="mm-lbl" style={{ marginTop: 4 }}>
                      aliases · {owner.aliases.join(", ")}
                    </div>
                  )}
                </div>
                <button
                  type="button"
                  className="mm-btn mm-btn-ghost mm-btn-sm"
                  onClick={() => void onClearOwner()}
                >
                  ✕ clear
                </button>
              </div>
            ) : (
              <p style={{ margin: "10px 0 0", color: "var(--mm-ink-3)", fontSize: 13 }}>
                Tell MeetingMind who you are and your actions, mentions, and workstreams will
                surface first across every screen. Pick yourself from the directory below.
              </p>
            )}
          </div>

          {orphanCount > 0 && (
            <div
              className="mm-row"
              style={{
                justifyContent: "space-between",
                alignItems: "center",
                marginTop: 14,
                padding: "10px 14px",
                background: "var(--mm-bone-2)",
                border: "1px dashed var(--mm-rule)",
                borderRadius: 8,
              }}
            >
              <span className="mm-lbl">
                {orphanCount} {orphanCount === 1 ? "person has" : "people have"} no meetings or actions
              </span>
              <button
                type="button"
                className="mm-btn mm-btn-sm"
                onClick={onPruneOrphans}
                title="Remove every person with no confirmed meetings and no actions"
              >
                ✕ Tidy ghosts
              </button>
            </div>
          )}
          <div className="mm-people-grid" style={{ marginTop: 18 }}>
            {sortedPeople.map((person) => {
              const isYou = !!person.is_you;
              return (
                <div
                  key={person.id}
                  className={isYou ? "mm-people-card is-you" : "mm-people-card"}
                  onClick={() => onSelectPerson(person.id)}
                  role="button"
                  tabIndex={0}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault();
                      onSelectPerson(person.id);
                    }
                  }}
                >
                  <SpeakerAvatar
                    name={person.display_name}
                    speakerNumber={(person.id % 6) + 1}
                    size={44}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="mm-display" style={{ fontSize: 20, lineHeight: 1.1 }}>
                      {person.display_name}
                      {isYou && <span className="mm-pill-yours" style={{ marginLeft: 6 }}>⌖ you</span>}
                    </div>
                    {person.role && (
                      <div className="mm-lbl" style={{ marginTop: 4, fontSize: 9 }}>
                        {person.role}
                      </div>
                    )}
                    <div className="mm-row" style={{ gap: 6, marginTop: 8 }}>
                      <Pill tone="quiet">{person.meeting_count}m</Pill>
                      <Pill tone="quiet">
                        {person.action_count}{" "}
                        {person.action_count === 1 ? "action" : "actions"}
                      </Pill>
                    </div>
                  </div>
                  {/* Inline "this is me" button removed — identity claim
                   * lives in the onboarding modal + sidebar identity chip
                   * now. A per-card button was redundant once the user
                   * had set their identity once. */}
                </div>
              );
            })}
            {!people.length && (
              <p className="mm-empty mm-empty-center" style={{ gridColumn: "1 / -1" }}>
                No people yet — names appear here once you confirm speaker assignments on a meeting.
              </p>
            )}
          </div>
        </>
      )}
    </section>
  );
}

// ── Archive screen (16-week heatmap + recent reel) ────────────────────────
function ArchiveScreen({
  timeline,
  onOpenMeeting,
  onRefresh,
}: {
  timeline: ArchiveTimeline | null;
  onOpenMeeting: (id: number) => void;
  onRefresh: () => Promise<void>;
}) {
  useEffect(() => {
    void onRefresh();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  if (!timeline) {
    return (
      <section className="mm-screen">
        <p className="mm-empty mm-empty-center">Loading archive…</p>
      </section>
    );
  }

  const peak = Math.max(1, ...timeline.cells.flat());
  const dayLabels = ["M", "Tu", "W", "Th", "F", "Sa", "Su"];

  return (
    <section className="mm-screen">
      <div className="mm-screen-header">
        <div>
          <div className="mm-lbl-strong">05 · history</div>
          <h1>
            The <Ital>archive</Ital>
          </h1>
          <p className="mm-screen-sub">
            Sixteen weeks of meeting density, your dominant voice, and your heaviest workstream.
          </p>
        </div>
        <div className="mm-screen-actions">
          <button type="button" className="mm-btn" onClick={() => void onRefresh()}>
            ↻ Refresh
          </button>
        </div>
      </div>
      <hr className="mm-rule" style={{ margin: "0 0 26px" }} />

      <div className="mm-archive-grid">
        <div className="mm-panel">
          <div className="mm-row" style={{ justifyContent: "space-between", alignItems: "baseline" }}>
            <div className="mm-display" style={{ fontSize: 26 }}>
              Sixteen <Ital>weeks</Ital>
            </div>
            <div className="mm-lbl">
              {timeline.total_meetings} note{timeline.total_meetings === 1 ? "" : "s"} ·{" "}
              {Math.round(timeline.total_minutes)}m
            </div>
          </div>
          <div className="mm-heatmap">
            {timeline.cells.map((row, rowIdx) => (
              <div key={rowIdx} className="mm-heatmap-row">
                <span className="mm-heatmap-label">{dayLabels[rowIdx]}</span>
                {row.map((count, colIdx) => {
                  const intensity = count === 0 ? 0 : 0.25 + (count / peak) * 0.75;
                  return (
                    <span
                      key={colIdx}
                      className="mm-heatmap-cell"
                      title={
                        count
                          ? `${count} meeting${count === 1 ? "" : "s"}`
                          : "no meetings"
                      }
                      style={{
                        background:
                          count === 0
                            ? "var(--mm-bone-3)"
                            : "var(--mm-clay)",
                        opacity: count === 0 ? 0.45 : intensity,
                      }}
                    />
                  );
                })}
              </div>
            ))}
          </div>
          <hr className="mm-rule" style={{ margin: "22px 0 18px" }} />
          <div className="mm-archive-stats">
            <div>
              <div className="mm-lbl">most active voice</div>
              <div className="mm-display" style={{ fontSize: 22, marginTop: 4 }}>
                {timeline.top_speaker || "—"}
              </div>
            </div>
            <div>
              <div className="mm-lbl">heaviest workstream</div>
              <div className="mm-display" style={{ fontSize: 22, marginTop: 4 }}>
                {timeline.top_workstream || "—"}
              </div>
              {timeline.top_workstream && (
                <div className="mm-mono" style={{ fontSize: 11, color: "var(--mm-ink-3)" }}>
                  {Math.round(timeline.top_workstream_confidence * 100)}% avg confidence
                </div>
              )}
            </div>
            <div>
              <div className="mm-lbl">average per week</div>
              <div className="mm-display" style={{ fontSize: 22, marginTop: 4 }}>
                {(timeline.total_meetings / timeline.weeks).toFixed(1)}
              </div>
            </div>
          </div>
        </div>

        <div>
          <div className="mm-lbl-strong">Recent reel</div>
          <hr className="mm-rule" style={{ margin: "10px 0 14px" }} />
          {timeline.recent.length ? (
            timeline.recent.map((meeting) => (
              <button
                key={meeting.id}
                type="button"
                className="mm-archive-reel-row"
                onClick={() => onOpenMeeting(meeting.id)}
              >
                <div style={{ textAlign: "right" }}>
                  <div className="mm-mono" style={{ fontSize: 12, fontWeight: 500 }}>
                    {formatDate(meeting.created_at)}
                  </div>
                  <div className="mm-lbl" style={{ marginTop: 3, fontSize: 9 }}>
                    {meeting.duration_minutes} min
                  </div>
                </div>
                <div style={{ flex: 1, minWidth: 0 }}>
                  <div className="mm-display" style={{ fontSize: 17, letterSpacing: "-0.01em" }}>
                    {meeting.title}
                  </div>
                  <div className="mm-lbl" style={{ marginTop: 4, fontSize: 9 }}>
                    {meeting.status}
                  </div>
                </div>
                <span className="mm-ital" style={{ fontSize: 13, color: "var(--mm-ink-3)" }}>
                  open →
                </span>
              </button>
            ))
          ) : (
            <p className="mm-empty">Nothing in the archive yet.</p>
          )}
        </div>
      </div>
    </section>
  );
}

function paletteActionKey(action: PaletteAction): string {
  switch (action.kind) {
    case "meeting":
      return `meeting:${action.id}`;
    case "person":
      return `person:${action.id}`;
    case "workstream":
      return `workstream:${action.name}`;
    case "view":
      return `view:${action.target}`;
    case "me-actions":
      return "me:actions";
  }
}

// ── Command palette (⌘K) ──────────────────────────────────────────────────
type PaletteAction =
  | { kind: "meeting"; id: number; title: string }
  | { kind: "person"; id: number; display_name: string }
  | { kind: "workstream"; name: string }
  | { kind: "view"; target: SidebarKey; label: string }
  | { kind: "me-actions" };

function CommandPalette({
  meetings,
  people,
  workstreams,
  owner,
  recentIds,
  onClose,
  onAction,
}: {
  meetings: Meeting[];
  people: PersonSummary[];
  workstreams: WorkstreamIntelligence[];
  owner: OwnerIdentity;
  recentIds: string[];
  onClose: () => void;
  onAction: (action: PaletteAction) => void;
}) {
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    inputRef.current?.focus();
  }, []);

  const items: PaletteAction[] = useMemo(() => {
    const q = query.trim().toLowerCase();
    const collected: PaletteAction[] = [];
    // "@me …" prefix mode: show owner-scoped shortcuts first.
    if (q.startsWith("@me") || q === "@") {
      if (owner.configured) {
        collected.push({ kind: "me-actions" });
        if (owner.person_id) {
          collected.push({
            kind: "person",
            id: owner.person_id,
            display_name: owner.display_name || "you",
          });
        }
      }
    }
    const views: Array<{ target: SidebarKey; label: string }> = [
      { target: "inbox", label: "Go to Inbox" },
      { target: "review", label: "Go to Review" },
      { target: "workstreams", label: "Go to Workstreams" },
      { target: "people", label: "Go to People" },
      { target: "archive", label: "Go to Archive" },
      { target: "settings", label: "Go to Settings" },
    ];
    views.forEach((view) => {
      if (!q || view.label.toLowerCase().includes(q)) {
        collected.push({ kind: "view", target: view.target, label: view.label });
      }
    });
    meetings.forEach((meeting) => {
      if (!q || meeting.title.toLowerCase().includes(q)) {
        collected.push({ kind: "meeting", id: meeting.id, title: meeting.title });
      }
    });
    people.forEach((person) => {
      if (!q || person.display_name.toLowerCase().includes(q)) {
        collected.push({ kind: "person", id: person.id, display_name: person.display_name });
      }
    });
    workstreams.forEach((stream) => {
      if (!q || stream.display_name.toLowerCase().includes(q)) {
        collected.push({ kind: "workstream", name: stream.display_name });
      }
    });
    // Most-recent-at-top: when query is empty, lift recently-actioned items.
    if (!q && recentIds.length) {
      const rank = (action: PaletteAction): number => {
        const key = paletteActionKey(action);
        const idx = recentIds.indexOf(key);
        return idx === -1 ? 999 : idx;
      };
      collected.sort((a, b) => rank(a) - rank(b));
    }
    return collected.slice(0, 30);
  }, [query, meetings, people, workstreams, owner, recentIds]);

  useEffect(() => {
    setCursor(0);
  }, [query]);

  const onKeyDown = (event: React.KeyboardEvent<HTMLInputElement>) => {
    if (event.key === "ArrowDown") {
      event.preventDefault();
      setCursor((c) => Math.min(items.length - 1, c + 1));
    } else if (event.key === "ArrowUp") {
      event.preventDefault();
      setCursor((c) => Math.max(0, c - 1));
    } else if (event.key === "Enter") {
      event.preventDefault();
      const selected = items[cursor];
      if (selected) onAction(selected);
    }
  };

  return (
    <div className="mm-modal-backdrop" onClick={onClose}>
      <div
        className="mm-palette"
        onClick={(event) => event.stopPropagation()}
        role="dialog"
        aria-label="Command palette"
      >
        <input
          ref={inputRef}
          className="mm-palette-input"
          placeholder="Search meetings, people, workstreams, or jump to a view…"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="mm-palette-list">
          {items.map((item, index) => {
            const label =
              item.kind === "meeting"
                ? item.title
                : item.kind === "person"
                  ? item.display_name
                  : item.kind === "workstream"
                    ? item.name
                    : item.kind === "me-actions"
                      ? `My open actions (across all meetings)`
                      : item.label;
            const kindLabel =
              item.kind === "meeting"
                ? "meeting"
                : item.kind === "person"
                  ? "person"
                  : item.kind === "workstream"
                    ? "workstream"
                    : item.kind === "me-actions"
                      ? "@me"
                      : "view";
            return (
              <button
                key={`${item.kind}-${index}`}
                type="button"
                className={
                  index === cursor ? "mm-palette-item is-active" : "mm-palette-item"
                }
                onMouseEnter={() => setCursor(index)}
                onClick={() => onAction(item)}
              >
                <span className="mm-lbl" style={{ width: 80, fontSize: 9 }}>
                  {kindLabel}
                </span>
                <span style={{ flex: 1 }}>{label}</span>
              </button>
            );
          })}
          {!items.length && (
            <div className="mm-palette-empty">no matches — try a different search</div>
          )}
        </div>
        <div className="mm-palette-foot">
          <span>↑↓ navigate</span>
          <span>↵ open</span>
          <span>esc close</span>
        </div>
      </div>
    </div>
  );
}

// ── Settings screen ───────────────────────────────────────────────────────
function SettingsScreen({
  health,
  settings,
  draft,
  setDraft,
  templates,
  maintenanceEnabled,
  maintenanceStatus,
  maintenanceTime,
  setMaintenanceTime,
  onSaveSettings,
  onConfigureMaintenance,
  onRefreshSettings,
  onBackendBouncing,
  settingsErrors,
}: {
  health: HealthStatus | null;
  settings: DashboardSettings | null;
  draft: SettingsDraft | null;
  setDraft: (draft: SettingsDraft | null) => void;
  templates: TemplateOption[];
  maintenanceEnabled: boolean;
  maintenanceStatus: string;
  maintenanceTime: string;
  setMaintenanceTime: (value: string) => void;
  onSaveSettings: () => Promise<void>;
  onConfigureMaintenance: (enabled: boolean) => Promise<void>;
  onRefreshSettings: () => Promise<void>;
  onBackendBouncing: (bouncing: boolean) => void;
  settingsErrors: string[];
}) {
  if (!settings || !draft) {
    return (
      <section className="mm-screen">
        <p className="mm-empty mm-empty-center">Loading local settings.</p>
      </section>
    );
  }
  // Datalist autocomplete options vary by provider. OpenRouter is a
  // multi-model gateway — we list a short curated set rather than the
  // full catalog; user can always type a model id by hand.
  const openrouterCurated = [
    "tencent/hy3-preview",
    "anthropic/claude-opus-4.7",
    "anthropic/claude-sonnet-4.6",
    "anthropic/claude-haiku-4.5",
    "openai/gpt-5",
    "openai/gpt-5-mini",
    "google/gemini-2.5-pro",
    "meta-llama/llama-3.1-405b-instruct",
    "qwen/qwen3-72b-instruct",
  ];
  const modelOptions =
    draft.modelProvider === "ollama"
      ? settings.models.ollama_models
      : draft.modelProvider === "openrouter"
        ? openrouterCurated
        : settings.models.lm_studio_models;
  const savedDefault = settings.models.default_model;
  const savedQuality = settings.models.quality_model;
  const draftDiffersFromSaved =
    draft.defaultModel !== savedDefault || draft.qualityModel !== savedQuality;
  const updateDraft = (patch: Partial<SettingsDraft>) => setDraft({ ...draft, ...patch });

  return (
    <section className="mm-screen">
      <div className="mm-screen-header">
        <div>
          <div className="mm-lbl-strong">09 · controls</div>
          <h1>
            The <Ital>settings</Ital>
          </h1>
          <p className="mm-screen-sub">
            Local-only configuration and runtime status. Nothing here leaves your machine.
          </p>
        </div>
        <div className="mm-screen-actions">
          <button type="button" className="mm-btn" onClick={() => void onRefreshSettings()}>
            ↻ Refresh
          </button>
          <RestartSystemButton
            onAfterRestart={() => void onRefreshSettings()}
            onBouncing={onBackendBouncing}
          />
        </div>
      </div>
      <hr className="mm-rule" style={{ margin: "0 0 26px" }} />

      <div className="mm-settings">
        <div className="mm-panel">
          <div className="mm-settings-head">
            <div>
              <h2>
                Local <Ital>runtime</Ital>
              </h2>
              <p>Local addresses and paths. Port changes take effect after restart.</p>
            </div>
            <Pill tone="sage" dot>
              {health?.status || "online"}
            </Pill>
          </div>
          <div className="mm-settings-body">
            <label className="mm-field">
              Dashboard port
              <input
                className="mm-input mm-input-square"
                type="number"
                min={1024}
                max={65535}
                value={draft.dashboardPort}
                onChange={(event) => updateDraft({ dashboardPort: event.target.value })}
              />
              <small>Save, then close and reopen MeetingMind to bind the new dashboard port.</small>
            </label>
            <label className="mm-field">
              Backend port
              <input
                className="mm-input mm-input-square"
                type="number"
                min={1024}
                max={65535}
                value={draft.backendPort}
                onChange={(event) => updateDraft({ backendPort: event.target.value })}
              />
              <small>Save, then restart MeetingMind to bind the new backend port.</small>
            </label>
            <SettingsValue label="Dashboard URL" value={settings.dashboard.url} />
            <SettingsValue label="Backend URL" value={settings.backend.url} />
            <SettingsValue label="Config file" value={settings.config_path} />
            <SettingsValue label="Vault" value={health?.vault || "vault/meeting_mind"} />
          </div>
        </div>

        <div className="mm-panel">
          <div className="mm-settings-head">
            <div>
              <h2>Models</h2>
              <p>Provider and model used for summaries, workstreams, and note generation.</p>
            </div>
            <Pill tone="quiet">
              {draft.modelProvider === "lm_studio"
                ? "LM Studio · local"
                : draft.modelProvider === "ollama"
                  ? "Ollama · local"
                  : "OpenRouter · cloud"}
            </Pill>
          </div>
          <div className="mm-settings-body">
            <label className="mm-field">
              Provider
              <select
                className="mm-input mm-input-square"
                value={draft.modelProvider}
                onChange={(event) => {
                  // Switching provider resets the model field. Without this,
                  // an OpenRouter slug like `tencent/hy3-preview` would
                  // appear as a "(custom)" option in the Ollama dropdown,
                  // which is meaningless. Reset to empty so the user
                  // explicitly picks from the new provider's catalogue.
                  const nextProvider = event.target.value as
                    | "lm_studio"
                    | "ollama"
                    | "openrouter";
                  updateDraft({
                    modelProvider: nextProvider,
                    defaultModel: "",
                    qualityModel: "",
                  });
                }}
              >
                <option value="lm_studio">LM Studio · local-first</option>
                <option value="ollama">Ollama · local-first</option>
                <option value="openrouter">OpenRouter · cloud (BYO API key)</option>
              </select>
              <small>
                MeetingMind is local-first: audio + transcription stay on your
                machine. OpenRouter is an opt-in, no-subscription, no-lock-in
                cloud option for higher-quality synthesis — only the cleaned
                transcript text leaves your machine when selected.
              </small>
            </label>
            {draft.modelProvider === "openrouter" && (
              <OpenRouterKeyField
                envVar={settings.models.openrouter?.api_key_env || "OPENROUTER"}
                apiKeySet={settings.models.openrouter?.api_key_set || false}
                onAfterSave={() => void onRefreshSettings()}
              />
            )}
            <HuggingFaceTokenField
              tokenSet={settings.huggingface?.token_set || false}
              modelAccessUrls={settings.huggingface?.model_access_urls || []}
              onAfterSave={() => void onRefreshSettings()}
            />
            <label className="mm-field">
              Model
              {draft.modelProvider === "openrouter" ? (
                <>
                  <input
                    className="mm-input mm-input-square"
                    list="openrouter-curated"
                    placeholder="tencent/hy3-preview"
                    value={draft.defaultModel}
                    onChange={(event) =>
                      updateDraft({
                        defaultModel: event.target.value,
                        qualityModel: event.target.value,
                      })
                    }
                    autoComplete="off"
                    spellCheck={false}
                  />
                  <datalist id="openrouter-curated">
                    {openrouterCurated.map((id) => (
                      <option key={id} value={id} />
                    ))}
                  </datalist>
                  <small>
                    Currently saved: <code>{savedDefault || "—"}</code>
                    {draft.defaultModel !== savedDefault && " — unsaved change pending"}
                  </small>
                  <small style={{ marginTop: 6, lineHeight: 1.55 }}>
                    Paste any OpenRouter model id (e.g.{" "}
                    <code>openai/gpt-5.5</code>,{" "}
                    <code>anthropic/claude-sonnet-4.6</code>,{" "}
                    <code>qwen/qwen-3.6-72b-instruct</code>). MeetingMind
                    needs at least an 8B-parameter class model to produce
                    usable extracts; bigger reasoning models yield more
                    precise meeting summaries. Browse the full catalogue at{" "}
                    <a
                      href="https://openrouter.ai/models"
                      target="_blank"
                      rel="noreferrer"
                      style={{ color: "var(--mm-clay)" }}
                    >
                      openrouter.ai/models
                    </a>
                    .
                  </small>
                </>
              ) : (
                <>
                  <select
                    className="mm-input mm-input-square"
                    value={draft.defaultModel}
                    onChange={(event) =>
                      updateDraft({
                        defaultModel: event.target.value,
                        // Quality model tracks the primary model now that the
                        // UI is unified. Users with a custom quality model in
                        // local.toml keep that value until they pick a new
                        // primary here.
                        qualityModel: event.target.value,
                      })
                    }
                  >
                    <option value="" disabled>
                      {modelOptions.length === 0
                        ? draft.modelProvider === "lm_studio"
                          ? "No LM Studio models detected — open LM Studio once and refresh."
                          : "No Ollama models detected — pull one with `ollama pull`."
                        : "Pick a model…"}
                    </option>
                    {modelOptions.map((id) => (
                      <option key={id} value={id}>
                        {id}
                      </option>
                    ))}
                  </select>
                  <small>
                    Currently saved: <code>{savedDefault || "—"}</code>
                    {draft.defaultModel !== savedDefault && " — unsaved change pending"}
                  </small>
                  <small style={{ marginTop: 6, lineHeight: 1.55 }}>
                    MeetingMind needs at least an 8B-parameter class local
                    model to produce usable extracts (Gemma 4 9B, Llama 3.1
                    8B, Qwen 3 8B). Larger or frontier models yield more
                    precise meeting summaries — pick the largest one your
                    machine can run comfortably.
                  </small>
                </>
              )}
            </label>
            {draftDiffersFromSaved && (
              <p className="mm-empty" style={{ margin: "4px 0 0", padding: "8px 12px" }}>
                You have unsaved model changes. Click Save below to write them
                to config/local.toml — they apply to the next pipeline run.
              </p>
            )}
            {draft.modelProvider === "lm_studio" && (
              <label className="mm-field">
                LM Studio URL
                <input
                  className="mm-input mm-input-square"
                  value={draft.lmStudioBaseUrl}
                  onChange={(event) => updateDraft({ lmStudioBaseUrl: event.target.value })}
                />
              </label>
            )}
            {draft.modelProvider === "ollama" && (
              <label className="mm-field">
                Ollama URL
                <input
                  className="mm-input mm-input-square"
                  value={draft.ollamaBaseUrl}
                  onChange={(event) => updateDraft({ ollamaBaseUrl: event.target.value })}
                />
              </label>
            )}
            {draft.modelProvider !== "openrouter" && (
              <label className="mm-field">
                Model keep-alive seconds
                <input
                  className="mm-input mm-input-square"
                  type="number"
                  min={60}
                  value={draft.modelIdleTtlSeconds}
                  onChange={(event) => updateDraft({ modelIdleTtlSeconds: event.target.value })}
                />
                <small>
                  How long LM Studio / Ollama hold the model in memory after a
                  request. Not used by OpenRouter (cloud is stateless).
                </small>
              </label>
            )}
            <label className="mm-field">
              Temperature
              <input
                className="mm-input mm-input-square"
                type="number"
                step="0.05"
                min={0}
                max={2}
                value={draft.modelTemperature}
                onChange={(event) => updateDraft({ modelTemperature: event.target.value })}
              />
            </label>
          </div>
        </div>

        <div className="mm-panel">
          <div className="mm-settings-head">
            <div>
              <h2>
                Transcription <Ital>assist</Ital>
              </h2>
              <p>Low-friction transcript quality controls. Repair runs automatically when enabled.</p>
            </div>
            <Pill tone="quiet">experimental</Pill>
          </div>
          <div className="mm-settings-body is-single">
            <label className="mm-toggle-row">
              <input
                type="checkbox"
                checked={draft.autoSendToObsidian}
                onChange={(event) =>
                  updateDraft({ autoSendToObsidian: event.target.checked })
                }
              />
              <span>
                <strong style={{ color: "var(--mm-ink)" }}>Auto-send to Obsidian when ready</strong>
                <small>
                  Off by default. When on, MeetingMind writes the vault note
                  automatically as soon as extraction finishes and every
                  speaker has been confirmed. The "Send to Obsidian" button
                  still works for manual control.
                </small>
              </span>
            </label>
            <label className="mm-toggle-row">
              <input
                type="checkbox"
                checked={draft.autoAudioRepair}
                onChange={(event) => updateDraft({ autoAudioRepair: event.target.checked })}
              />
              <span>
                <strong style={{ color: "var(--mm-ink)" }}>
                  Experimental: multi-pass ASR repair
                </strong>
                <small>
                  Off by default. Adds 2-3 extra Whisper passes per meeting
                  to refine low-confidence spans — slower ingest, marginal
                  accuracy gains. Frontier-model synthesis often closes the
                  same gap downstream without the extra cost.
                </small>
              </span>
            </label>
            <label className="mm-toggle-row">
              <input
                type="checkbox"
                checked={draft.vocalPresentationCueScoring}
                onChange={(event) => updateDraft({ vocalPresentationCueScoring: event.target.checked })}
              />
              <span>
                <strong style={{ color: "var(--mm-ink)" }}>Voice cue assist</strong>
                <small>
                  Optional weak signal for speaker suggestions. Cannot identify anyone by itself.
                </small>
              </span>
            </label>
            {/* "Default template" Settings field removed — template is now
             * always chosen at ingest time (per file in the watch folder,
             * via a modal on direct upload). No default = user picks the
             * type when they have the most context about the file. */}
            <label className="mm-toggle-row">
              <input
                type="checkbox"
                checked={draft.showKeyTermHighlights}
                onChange={(event) =>
                  updateDraft({ showKeyTermHighlights: event.target.checked })
                }
              />
              <span>
                <strong style={{ color: "var(--mm-ink)" }}>Key-term highlights in transcript</strong>
                <small>
                  Off by default. When on, the model's suggested domain terms get a chartreuse mark inside transcript text.
                </small>
              </span>
            </label>
            <label className="mm-toggle-row">
              <input
                type="checkbox"
                checked={draft.showTranscriptConfidenceChips}
                onChange={(event) =>
                  updateDraft({ showTranscriptConfidenceChips: event.target.checked })
                }
              />
              <span>
                <strong style={{ color: "var(--mm-ink)" }}>Confidence chips on transcript rows</strong>
                <small>
                  Off by default. When on, each transcript row shows its content + speaker-assignment confidence. Useful while auditing quality, noisy in normal reading.
                </small>
              </span>
            </label>
            <details style={{ marginTop: 10 }}>
              <summary
                className="mm-lbl-strong"
                style={{ cursor: "pointer", listStyle: "none" }}
              >
                Optional recognition vocabulary
              </summary>
              <label className="mm-field" style={{ marginTop: 12 }}>
                Names, acronyms, project terms
                <textarea
                  className="mm-input"
                  value={draft.vocabularyTerms}
                  onChange={(event) => updateDraft({ vocabularyTerms: event.target.value })}
                  placeholder="One term per line, e.g. RevOps, MeetingMind, OKRs"
                />
                <small>
                  Saved to {settings.transcription.asr_vocabulary_path}. Stays gitignored.
                </small>
              </label>
            </details>
          </div>
        </div>

        <div className="mm-panel">
          <div className="mm-settings-head">
            <div>
              <h2>
                Daily <Ital>maintenance</Ital>
              </h2>
              <p>Service check, vault link audit, action-index refresh. Runs locally once a day.</p>
            </div>
            <Pill tone={maintenanceEnabled ? "sage" : "quiet"} dot={maintenanceEnabled}>
              {maintenanceEnabled ? "enabled" : "disabled"}
            </Pill>
          </div>
          <div className="mm-settings-body">
            <div>
              <div className="mm-display" style={{ fontSize: 20, marginBottom: 6 }}>
                What runs <Ital>daily</Ital>
              </div>
              <p style={{ fontSize: 13, color: "var(--mm-ink-2)", lineHeight: 1.55, margin: 0 }}>
                Health check, vault link lint, and action-rollup refresh. Last results: {maintenanceStatus || "not run"}.
              </p>
            </div>
            <div>
              <div className="mm-lbl-strong">Run time</div>
              <div className="mm-row" style={{ marginTop: 10, flexWrap: "wrap" }}>
                <input
                  className="mm-input mm-input-square mm-mono"
                  style={{ width: 120 }}
                  type="time"
                  value={maintenanceTime}
                  onChange={(event) => setMaintenanceTime(event.target.value)}
                />
                <button
                  type="button"
                  className="mm-btn mm-btn-primary"
                  onClick={() => void onConfigureMaintenance(true)}
                >
                  ✓ Save
                </button>
                <button
                  type="button"
                  className="mm-btn mm-btn-ghost"
                  onClick={() => void onConfigureMaintenance(false)}
                >
                  Disable
                </button>
              </div>
            </div>
          </div>
        </div>

        <div className="mm-settings-actions">
          {!!settingsErrors.length && (
            <div className="mm-errors" role="alert">
              {settingsErrors.map((error) => (
                <span key={error}>{error}</span>
              ))}
            </div>
          )}
          <button
            type="button"
            className="mm-btn mm-btn-clay"
            disabled={!!settingsErrors.length}
            onClick={() => void onSaveSettings()}
          >
            ✓ Save settings
          </button>
        </div>
      </div>
    </section>
  );
}

function SettingsValue({ label, value }: { label: string; value: string }) {
  return (
    <div className="mm-field">
      {label}
      <div
        className="mm-input mm-input-square mm-mono"
        style={{
          color: "var(--mm-ink-2)",
          background: "var(--mm-bone-3)",
          wordBreak: "break-all",
          fontSize: 12,
          minHeight: 38,
          display: "flex",
          alignItems: "center",
        }}
      >
        {value}
      </div>
    </div>
  );
}

// Editable OpenRouter API key field. Saves to .env.local via the backend
// `/api/settings/openrouter-key` endpoint and updates the in-process env
// var so the change takes effect on the very next extract call — no
// backend restart required.
function OpenRouterKeyField({
  envVar,
  apiKeySet,
  onAfterSave,
}: {
  envVar: string;
  apiKeySet: boolean;
  onAfterSave: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedHint, setSavedHint] = useState<string>("");
  const save = async () => {
    setSaving(true);
    setSavedHint("");
    try {
      await api.postJson<{ api_key_set: boolean }>(
        "/api/settings/openrouter-key",
        { api_key: draft },
      );
      setDraft("");
      setSavedHint(draft ? "Key saved. Active now." : "Key cleared.");
      onAfterSave();
    } catch (error) {
      setSavedHint(error instanceof Error ? error.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="mm-openrouter-card">
      <div className="mm-openrouter-row">
        <span className="mm-lbl">API key</span>
        {apiKeySet ? (
          <Pill tone="sage" dot>
            {envVar} · loaded
          </Pill>
        ) : (
          <Pill tone="clay" dot>
            {envVar} · missing
          </Pill>
        )}
      </div>
      <label className="mm-field" style={{ marginTop: 4 }}>
        Paste your OpenRouter key
        <input
          className="mm-input mm-input-square"
          type="password"
          placeholder={apiKeySet ? "•••••••• (key on file — replace to update)" : "sk-or-v1-..."}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <small>
          Saved to <code>.env.local</code> (gitignored) as{" "}
          <code>{envVar}=…</code>. Takes effect immediately — no restart.
          Get a key at{" "}
          <a
            href="https://openrouter.ai/keys"
            target="_blank"
            rel="noreferrer"
            style={{ color: "var(--mm-clay)" }}
          >
            openrouter.ai/keys
          </a>
          .
        </small>
      </label>
      <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
        <button
          type="button"
          className="mm-btn mm-btn-primary mm-btn-sm"
          onClick={() => void save()}
          disabled={saving || !draft.trim()}
        >
          {saving ? "Saving…" : "Save key"}
        </button>
        {apiKeySet && (
          <button
            type="button"
            className="mm-btn mm-btn-ghost mm-btn-sm"
            onClick={() => {
              setDraft("");
              void save();
            }}
            disabled={saving}
            title="Clear the stored key"
          >
            Clear
          </button>
        )}
        {savedHint && (
          <small style={{ opacity: 0.7 }}>{savedHint}</small>
        )}
      </div>
      <p className="mm-openrouter-warning">
        ⚠ When this provider is selected, the cleaned transcript text is
        sent to OpenRouter for inference. Audio + raw transcript stay
        local. No subscription, no lock-in.
      </p>
    </div>
  );
}

// Hugging Face token paste field. Mirrors the OpenRouter card —
// posts to /api/settings/huggingface-token which rewrites .env.local
// and loads the value into the running backend process so the
// pyannote download picks it up without a restart.
function HuggingFaceTokenField({
  tokenSet,
  modelAccessUrls,
  onAfterSave,
}: {
  tokenSet: boolean;
  modelAccessUrls: string[];
  onAfterSave: () => void;
}) {
  const [draft, setDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedHint, setSavedHint] = useState<string>("");
  const save = async () => {
    setSaving(true);
    setSavedHint("");
    try {
      await api.postJson<{ token_set: boolean }>(
        "/api/settings/huggingface-token",
        { token: draft },
      );
      setDraft("");
      setSavedHint(draft ? "Token saved. Active now." : "Token cleared.");
      onAfterSave();
    } catch (error) {
      setSavedHint(error instanceof Error ? error.message : "Save failed.");
    } finally {
      setSaving(false);
    }
  };
  return (
    <div className="mm-openrouter-card">
      <div className="mm-openrouter-row">
        <span className="mm-lbl">Hugging Face</span>
        {tokenSet ? (
          <Pill tone="sage" dot>
            HUGGING_FACE_HUB_TOKEN · loaded
          </Pill>
        ) : (
          <Pill tone="clay" dot>
            HUGGING_FACE_HUB_TOKEN · missing
          </Pill>
        )}
      </div>
      <label className="mm-field" style={{ marginTop: 4 }}>
        Paste your Hugging Face token
        <input
          className="mm-input mm-input-square"
          type="password"
          placeholder={tokenSet ? "•••••••• (token on file — replace to update)" : "hf_..."}
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          autoComplete="off"
          spellCheck={false}
        />
        <small>
          Saved to <code>.env.local</code> (gitignored) as{" "}
          <code>HUGGING_FACE_HUB_TOKEN=…</code>. Takes effect immediately —
          no restart. Get a token at{" "}
          <a
            href="https://huggingface.co/settings/tokens"
            target="_blank"
            rel="noreferrer"
            style={{ color: "var(--mm-clay)" }}
          >
            huggingface.co/settings/tokens
          </a>
          .
        </small>
      </label>
      <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
        <button
          type="button"
          className="mm-btn mm-btn-primary mm-btn-sm"
          onClick={() => void save()}
          disabled={saving || !draft.trim()}
        >
          {saving ? "Saving…" : "Save token"}
        </button>
        {tokenSet && (
          <button
            type="button"
            className="mm-btn mm-btn-ghost mm-btn-sm"
            onClick={() => {
              setDraft("");
              void save();
            }}
            disabled={saving}
            title="Clear the stored token"
          >
            Clear
          </button>
        )}
        {savedHint && <small style={{ opacity: 0.7 }}>{savedHint}</small>}
      </div>
      {modelAccessUrls.length > 0 && (
        <p className="mm-openrouter-warning" style={{ marginTop: 12 }}>
          ⚠ Diarization needs <strong>accepted access</strong> to both gated models.
          Open each page once while logged in to your Hugging Face account
          and click "Accept license":
          <br />
          {modelAccessUrls.map((url, i) => (
            <span key={url}>
              {i > 0 && " · "}
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--mm-clay)" }}
              >
                {url.replace("https://huggingface.co/", "")}
              </a>
            </span>
          ))}
        </p>
      )}
    </div>
  );
}

function ModelRecommendations({
  recs,
  available,
  onPick,
}: {
  recs: Array<{ id: string; tier: string; role: string; note: string }>;
  available: string[];
  onPick: (id: string) => void;
}) {
  if (!recs.length) return null;
  const availableSet = new Set(available);
  return (
    <div className="mm-model-recs">
      {recs.map((rec) => {
        const installed = availableSet.has(rec.id);
        const toneClass = rec.tier === "recommended" ? "mm-pill mm-pill-sage" : "mm-pill mm-pill-quiet";
        return (
          <button
            key={`${rec.role}-${rec.id}`}
            type="button"
            className="mm-btn mm-btn-ghost mm-model-rec"
            onClick={() => onPick(rec.id)}
            title={installed ? "Use this model" : "Not yet installed locally — picking will pre-fill the field"}
          >
            <div className="mm-model-rec-head">
              <span className={toneClass}>{rec.tier}</span>
              <code>{rec.id}</code>
            </div>
            <small className="mm-model-rec-note">
              {rec.note}
              {!installed && " · not installed yet"}
            </small>
          </button>
        );
      })}
    </div>
  );
}

function ModelInput({
  value,
  options,
  onChange,
}: {
  value: string;
  options: string[];
  onChange: (value: string) => void;
}) {
  const listId = `models-${options.length}-${value.replace(/[^a-z0-9]/gi, "-")}`;
  return (
    <>
      <input
        className="mm-input mm-input-square"
        list={listId}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
      <datalist id={listId}>
        {options.map((model) => (
          <option key={model} value={model} />
        ))}
      </datalist>
      {!options.length && <small>No running provider models detected; manual entry is allowed.</small>}
    </>
  );
}

// ── Helpers ────────────────────────────────────────────────────────────────
function clipChapterTitle(value: string): string {
  // Chapter rail is a narrow rail; takeaways and workstream descriptions
  // come back as full sentences. Take everything up to the first sentence
  // terminator (".!?") then hard-cap at 60 chars so the rail item reads
  // as a label, not a paragraph.
  const trimmed = (value || "").trim();
  if (!trimmed) return "Chapter";
  const sentenceEnd = trimmed.search(/[.!?]\s/);
  const candidate = sentenceEnd > 0 ? trimmed.slice(0, sentenceEnd) : trimmed;
  if (candidate.length <= 60) return candidate;
  const truncated = candidate.slice(0, 60).replace(/\s+\S*$/, "");
  return truncated + "…";
}

function chaptersFromMarkers(
  markers: Array<{ label: string; start_segment_id: number; summary?: string }>,
  segments: Segment[],
  overview: MeetingOverview,
  detailSegments: Segment[],
): Chapter[] {
  // Bin segments into per-marker ranges by walking the chronological
  // segments and grouping each into the latest open marker. Mirror the
  // structure deriveChapters historically produced so the rail + scrubber
  // tick code paths don't need changes.
  const sortedSegments = [...segments].sort((a, b) => a.start_ms - b.start_ms);
  const sortedMarkers = [...markers]
    .filter((m) => sortedSegments.some((s) => s.id === m.start_segment_id))
    .sort((a, b) => {
      const aStart = sortedSegments.find((s) => s.id === a.start_segment_id)?.start_ms ?? 0;
      const bStart = sortedSegments.find((s) => s.id === b.start_segment_id)?.start_ms ?? 0;
      return aStart - bStart;
    });
  if (!sortedMarkers.length) {
    return [];
  }
  // Build a marker index → list of segments contained in that chapter.
  const buckets: Segment[][] = sortedMarkers.map(() => []);
  let cursor = -1;
  sortedSegments.forEach((segment) => {
    if (cursor + 1 < sortedMarkers.length && segment.id === sortedMarkers[cursor + 1].start_segment_id) {
      cursor += 1;
    }
    if (cursor < 0) cursor = 0;
    buckets[cursor].push(segment);
  });
  void overview;
  void detailSegments;

  const chapters: Chapter[] = [];
  sortedMarkers.forEach((marker, index) => {
    const bucket = buckets[index];
    if (!bucket.length) return;
    const totalConf = bucket.reduce(
      (acc, s) => acc + (s.text_confidence ?? s.confidence ?? 0.85),
      0,
    );
    const reps = [...bucket]
      .sort((a, b) => b.text.length - a.text.length)
      .slice(0, 3)
      .sort((a, b) => a.start_ms - b.start_ms);
    const bullets: ChapterBullet[] = reps.map((segment) => ({
      text: segment.text,
      speakerId: segment.diarization_speaker_id,
      startMs: segment.start_ms,
    }));
    const wordsBySpeaker = new Map<string, number>();
    bucket.forEach((segment) => {
      const words = segment.text.split(/\s+/).length;
      wordsBySpeaker.set(
        segment.diarization_speaker_id,
        (wordsBySpeaker.get(segment.diarization_speaker_id) || 0) + words,
      );
    });
    const topSpeakerId =
      Array.from(wordsBySpeaker.entries()).sort((a, b) => b[1] - a[1])[0]?.[0] ||
      bucket[0].diarization_speaker_id;
    chapters.push({
      id: `marker-${index}`,
      title: marker.label,
      summary: marker.summary,
      startMs: bucket[0].start_ms,
      segmentIds: bucket.map((s) => s.id),
      avgConfidence: totalConf / Math.max(1, bucket.length),
      bullets,
      topSpeakerId,
    });
  });
  return chapters;
}

function deriveChapters(detail: MeetingDetail | null): Chapter[] {
  if (!detail) return [];
  const overview = detail.overview;
  const segments = detail.segments;
  if (!segments.length) return [];

  // Prefer model-generated chapter_markers — they're already constrained
  // to ≤7-word labels and anchored to a real segment_id. Fall back to
  // workstream/key-takeaway derivation only when the field is empty
  // (legacy extractions or models that didn't supply chapters).
  const markers = overview.chapter_markers || [];
  if (markers.length > 0) {
    return chaptersFromMarkers(markers, segments, overview, detail.segments);
  }

  // Legacy path: evenly map titles across segments.
  const rawTitles = overview.workstreams.length
    ? overview.workstreams
    : overview.key_takeaways.length
      ? overview.key_takeaways
      : [overview.title];
  const titles = rawTitles.map((value) => clipChapterTitle(value));

  const chunkSize = Math.max(1, Math.ceil(segments.length / titles.length));
  const chapters: Chapter[] = [];
  titles.forEach((title, index) => {
    const chunk = segments.slice(index * chunkSize, (index + 1) * chunkSize);
    if (!chunk.length) {
      // Don't emit chapters that map to no segments — they'd seek the audio
      // back to 00:00 on click, which is misleading.
      return;
    }
    const totalConf = chunk.reduce(
      (acc, segment) => acc + (segment.text_confidence ?? segment.confidence ?? 0.85),
      0,
    );
    // Pick representative segments by length, preserving chronological order.
    const representatives = [...chunk]
      .sort((a, b) => b.text.length - a.text.length)
      .slice(0, 3)
      .sort((a, b) => a.start_ms - b.start_ms);
    // Detailed Minutes renders these bullets verbatim, so don't truncate here.
    // Callers that need a short preview (e.g. pull-quote) apply their own clip.
    const bullets: ChapterBullet[] = representatives.map((segment) => ({
      text: segment.text,
      speakerId: segment.diarization_speaker_id,
      startMs: segment.start_ms,
    }));
    // Heuristic for chapter "owner": the speaker with the most words in the chunk.
    const wordsBySpeaker = new Map<string, number>();
    chunk.forEach((segment) => {
      const words = segment.text.split(/\s+/).length;
      wordsBySpeaker.set(
        segment.diarization_speaker_id,
        (wordsBySpeaker.get(segment.diarization_speaker_id) || 0) + words,
      );
    });
    const topSpeakerId =
      Array.from(wordsBySpeaker.entries()).sort((a, b) => b[1] - a[1])[0]?.[0] ||
      chunk[0].diarization_speaker_id;
    chapters.push({
      id: `${index}`,
      title: title.trim() || `Chapter ${index + 1}`,
      startMs: chunk[0].start_ms,
      segmentIds: chunk.map((segment) => segment.id),
      avgConfidence: totalConf / chunk.length,
      bullets,
      topSpeakerId,
    });
  });
  return chapters;
}

function averageConfidence(chapters: Chapter[]) {
  if (!chapters.length) return 0;
  return chapters.reduce((acc, chapter) => acc + chapter.avgConfidence, 0) / chapters.length;
}

function accentHeadline(title: string): React.ReactNode {
  if (!title) return "Untitled meeting";
  const words = title.split(/\s+/);
  if (words.length < 2) return title;
  const accentIndex = words.findIndex((word) => word.length > 3) ?? 1;
  return words.map((word, index) => {
    if (index === accentIndex) {
      return (
        <React.Fragment key={index}>
          {index > 0 && " "}
          <Ital>{word}</Ital>
        </React.Fragment>
      );
    }
    return (
      <React.Fragment key={index}>
        {index > 0 && " "}
        {word}
      </React.Fragment>
    );
  });
}

function settingsToDraft(settings: DashboardSettings): SettingsDraft {
  return {
    dashboardPort: String(settings.dashboard.port),
    backendPort: String(settings.backend.port),
    modelProvider: settings.models.provider,
    defaultModel: settings.models.default_model,
    qualityModel: settings.models.quality_model,
    lmStudioBaseUrl: settings.models.lm_studio_base_url,
    ollamaBaseUrl: settings.models.ollama_base_url,
    modelIdleTtlSeconds: String(settings.models.idle_ttl_seconds),
    modelTemperature: String(settings.models.temperature),
    autoAudioRepair: settings.transcription.auto_audio_repair,
    vocalPresentationCueScoring: settings.transcription.vocal_presentation_cue_scoring,
    showKeyTermHighlights: settings.dashboard_prefs?.show_key_term_highlights ?? false,
    showTranscriptConfidenceChips:
      settings.dashboard_prefs?.show_transcript_confidence_chips ?? false,
    defaultTemplate: settings.dashboard_prefs?.default_template || "general",
    autoSendToObsidian: settings.dashboard_prefs?.auto_send_to_obsidian ?? false,
    vocabularyTerms: settings.transcription.asr_vocabulary_terms.join("\n"),
  };
}

function validateSettingsDraft(draft: SettingsDraft | null): string[] {
  if (!draft) return [];
  const errors: string[] = [];
  const dashboardPort = Number(draft.dashboardPort);
  const backendPort = Number(draft.backendPort);
  const keepAliveSeconds = Number(draft.modelIdleTtlSeconds);
  const temperature = Number(draft.modelTemperature);
  if (!Number.isInteger(dashboardPort) || dashboardPort < 1024 || dashboardPort > 65535) {
    errors.push("Dashboard port must be 1024-65535.");
  }
  if (!Number.isInteger(backendPort) || backendPort < 1024 || backendPort > 65535) {
    errors.push("Backend port must be 1024-65535.");
  }
  if (!draft.defaultModel.trim()) errors.push("Primary model is required.");
  for (const [label, value] of [
    ["LM Studio URL", draft.lmStudioBaseUrl],
    ["Ollama URL", draft.ollamaBaseUrl],
  ] as const) {
    if (!/^https?:\/\/.+/i.test(value.trim())) errors.push(`${label} must start with http:// or https://.`);
  }
  if (!Number.isInteger(keepAliveSeconds) || keepAliveSeconds < 60 || keepAliveSeconds > 86400) {
    errors.push("Model keep-alive must be 60-86400 seconds.");
  }
  if (!Number.isFinite(temperature) || temperature < 0 || temperature > 2) {
    errors.push("Temperature must be between 0 and 2.");
  }
  return errors;
}

function parseSegmentIds(value: string | undefined) {
  if (!value) return [];
  try {
    const parsed = JSON.parse(value);
    return Array.isArray(parsed) ? parsed.filter((item) => Number.isInteger(item)) : [];
  } catch {
    return [];
  }
}

function orderedSpeakerIds(segments: Segment[]) {
  const firstSeen = new Map<string, number>();
  segments.forEach((segment) => {
    if (!firstSeen.has(segment.diarization_speaker_id)) {
      firstSeen.set(segment.diarization_speaker_id, segment.start_ms);
    }
  });
  return Array.from(firstSeen.entries())
    .sort((left, right) => left[1] - right[1] || left[0].localeCompare(right[0]))
    .map(([speakerId]) => speakerId);
}

function speakerAliasMap(speakerIds: string[]) {
  return new Map(speakerIds.map((speakerId, index) => [speakerId, `Speaker ${index + 1}`]));
}

function bestSpeakerSuggestionMap(suggestions: SpeakerSuggestion[]) {
  const best = new Map<string, SpeakerSuggestion>();
  suggestions.forEach((suggestion) => {
    const existing = best.get(suggestion.speakerId);
    if (!existing || suggestion.confidence > existing.confidence) {
      best.set(suggestion.speakerId, suggestion);
    }
  });
  return best;
}

function displaySpeakerNameMap(
  speakerIds: string[],
  aliases: Map<string, string>,
  approvedLabels: Map<string, string>,
  suggestions: Map<string, SpeakerSuggestion>,
  suggestionThreshold: number,
) {
  return new Map(
    speakerIds.map((speakerId) => {
      const alias = aliases.get(speakerId) || speakerId;
      const approvedLabel = approvedLabels.get(speakerId);
      if (approvedLabel && approvedLabel !== speakerId && approvedLabel !== alias) {
        return [speakerId, approvedLabel];
      }
      const suggestion = suggestions.get(speakerId);
      if (suggestion && suggestion.confidence >= suggestionThreshold) {
        return [speakerId, suggestion.candidateName];
      }
      return [speakerId, alias];
    }),
  );
}

function savedSpeakerIdSet(
  speakerIds: string[],
  aliases: Map<string, string>,
  approvedLabels: Map<string, string>,
) {
  return new Set(
    speakerIds.filter((speakerId) => {
      const label = approvedLabels.get(speakerId)?.trim();
      const alias = aliases.get(speakerId) || speakerId;
      return Boolean(label && label !== speakerId && label !== alias);
    }),
  );
}

function parseSpeakerSuggestion(item: ReviewItem): SpeakerSuggestion | null {
  if (!item.payload_json) return null;
  try {
    const payload = JSON.parse(item.payload_json);
    const speakerId = String(payload.speaker_id || "").trim();
    const candidateName = String(payload.candidate_name || "").trim();
    if (!speakerId || !candidateName) return null;
    const evidence = Array.isArray(payload.evidence)
      ? payload.evidence
          .map((entry: { phrase?: unknown; evidence_type?: unknown }) =>
            [entry.evidence_type, entry.phrase].filter(Boolean).join(": "),
          )
          .filter(Boolean)
      : [];
    const profileSummary = payload.profile_summary
      ? `Seen in ${payload.profile_summary.meeting_count || 0} prior meeting(s).`
      : "";
    return {
      item,
      speakerId,
      candidateName,
      confidence: item.confidence ?? 0.5,
      basis: String(payload.confidence_basis || profileSummary || "Conversation evidence"),
      evidence: evidence.length ? evidence : [profileSummary].filter(Boolean),
      sourceSegmentIds: parseSegmentIds(item.source_segment_ids),
    };
  } catch {
    return null;
  }
}

function parseSummaryText(payload: string | undefined) {
  if (!payload) return "";
  try {
    const parsed = JSON.parse(payload);
    return typeof parsed.summary === "string" ? parsed.summary : "";
  } catch {
    return "";
  }
}

function parseCandidateMetrics(payload: string | undefined) {
  if (!payload) return {};
  try {
    const parsed = JSON.parse(payload);
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed)
        .filter(([, value]) => value !== null && value !== undefined && value !== "")
        .slice(0, 6),
    );
  } catch {
    return {};
  }
}

function formatMetricLabel(value: string) {
  return value.replaceAll("_", " ");
}

function candidateChangeSnippets(currentText: string, candidateText: string) {
  const currentWords = currentText.split(/\s+/).filter(Boolean);
  const candidateWords = candidateText.split(/\s+/).filter(Boolean);
  if (!currentWords.length || !candidateWords.length) return [];
  const rows = Array.from({ length: currentWords.length + 1 }, () =>
    Array(candidateWords.length + 1).fill(0) as number[],
  );
  for (let left = currentWords.length - 1; left >= 0; left -= 1) {
    for (let right = candidateWords.length - 1; right >= 0; right -= 1) {
      if (normalizeDiffWord(currentWords[left]) === normalizeDiffWord(candidateWords[right])) {
        rows[left][right] = rows[left + 1][right + 1] + 1;
      } else {
        rows[left][right] = Math.max(rows[left + 1][right], rows[left][right + 1]);
      }
    }
  }
  const parts: Array<{ kind: "same" | "removed" | "added"; word: string }> = [];
  let left = 0;
  let right = 0;
  while (left < currentWords.length && right < candidateWords.length) {
    if (normalizeDiffWord(currentWords[left]) === normalizeDiffWord(candidateWords[right])) {
      parts.push({ kind: "same", word: candidateWords[right] });
      left += 1;
      right += 1;
    } else if (rows[left + 1][right] >= rows[left][right + 1]) {
      parts.push({ kind: "removed", word: currentWords[left] });
      left += 1;
    } else {
      parts.push({ kind: "added", word: candidateWords[right] });
      right += 1;
    }
  }
  while (left < currentWords.length) {
    parts.push({ kind: "removed", word: currentWords[left] });
    left += 1;
  }
  while (right < candidateWords.length) {
    parts.push({ kind: "added", word: candidateWords[right] });
    right += 1;
  }
  const windows: React.ReactNode[] = [];
  const changedIndexes = parts
    .map((part, index) => (part.kind === "same" ? -1 : index))
    .filter((index) => index >= 0);
  const used = new Set<number>();
  changedIndexes.slice(0, 8).forEach((index) => {
    const start = Math.max(0, index - 4);
    const end = Math.min(parts.length, index + 5);
    const key = `${start}-${end}`;
    if (used.has(start)) return;
    used.add(start);
    windows.push(
      <span key={key} className="mm-candidate-change-window">
        {start > 0 && <em>...</em>}
        {parts.slice(start, end).map((part, partIndex) => (
          <mark key={`${key}-${partIndex}`} className={part.kind}>
            {part.word}
          </mark>
        ))}
        {end < parts.length && <em>...</em>}
      </span>,
    );
  });
  return windows;
}

function normalizeDiffWord(value: string) {
  return value.toLowerCase().replace(/[^a-z0-9']/g, "");
}

createRoot(document.getElementById("root")!).render(<App />);

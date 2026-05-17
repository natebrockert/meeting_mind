import React, { memo, useEffect, useRef, useState } from "react";

import { api } from "../api";
import { formatSeconds } from "../format";
import type { WaveformData } from "../types";

// Renders one <g> for played bars and one for unplayed bars, both filling
// the entire waveform width. The played/unplayed split is achieved by the
// parent's <clipPath> defs, which DO change per playhead tick — but
// SVG clipping is cheap, far cheaper than rebuilding hundreds of <rect>
// elements on every animation frame. React.memo'd against `peaks` identity
// + geometry so the bars are only diff'd when the meeting changes.
const WaveformPeaks = memo(function WaveformPeaks({
  peaks,
  height,
  half,
  bucketWidth,
}: {
  peaks: number[];
  height: number;
  half: number;
  bucketWidth: number;
}) {
  const bars = peaks.map((peak, index) => {
    const x = index * bucketWidth;
    const barHeight = Math.max(1, peak * (height - 4));
    return (
      <rect
        key={index}
        x={x}
        y={half - barHeight / 2}
        width={Math.max(1, bucketWidth - 0.5)}
        height={barHeight}
      />
    );
  });
  return (
    <>
      <g clipPath="url(#mm-wave-played-clip)" fill="var(--mm-clay)" opacity={0.95}>
        {bars}
      </g>
      <g clipPath="url(#mm-wave-unplayed-clip)" fill="var(--mm-ink-2)" opacity={0.85}>
        {bars}
      </g>
    </>
  );
});

export function Waveform({
  meetingId,
  totalSeconds,
  playheadSeconds,
  onSeek,
}: {
  meetingId: number;
  totalSeconds: number;
  playheadSeconds: number;
  onSeek: (seconds: number) => void;
}) {
  const [data, setData] = useState<WaveformData | null>(null);
  const [hover, setHover] = useState<{ x: number; seconds: number } | null>(null);
  const wrapRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    void api
      .get<WaveformData>(`/api/meetings/${meetingId}/waveform`)
      .then((result) => {
        if (!cancelled) setData(result);
      })
      .catch(() => {
        if (!cancelled) setData(null);
      });
    return () => {
      cancelled = true;
    };
  }, [meetingId]);

  const handleSeek = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!data || totalSeconds <= 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    onSeek(Math.max(0, Math.min(totalSeconds, totalSeconds * ratio)));
  };

  const handleHover = (event: React.MouseEvent<SVGSVGElement>) => {
    if (!data || totalSeconds <= 0) return;
    const rect = event.currentTarget.getBoundingClientRect();
    const x = event.clientX - rect.left;
    const ratio = x / rect.width;
    setHover({ x, seconds: Math.max(0, Math.min(totalSeconds, totalSeconds * ratio)) });
  };

  if (!data || !data.peaks.length || totalSeconds <= 0) return null;
  const width = 1000;
  const height = 64;
  const half = height / 2;
  const bucketWidth = width / data.peaks.length;
  const playheadRatio = Math.max(0, Math.min(1, playheadSeconds / totalSeconds));
  const speakerColors = new Map<string, string>();
  const speakerSlot = new Map<string, number>();
  data.speaker_segments.forEach((segment) => {
    if (!speakerSlot.has(segment.speaker_id)) {
      const slot = (speakerSlot.size % 6) + 1;
      speakerSlot.set(segment.speaker_id, slot);
      speakerColors.set(segment.speaker_id, `var(--mm-spk-${slot}-fg)`);
    }
  });
  return (
    <div className="mm-waveform" ref={wrapRef}>
      <svg
        viewBox={`0 0 ${width} ${height}`}
        preserveAspectRatio="none"
        role="slider"
        aria-label="Audio waveform — click to seek"
        onClick={handleSeek}
        onMouseMove={handleHover}
        onMouseLeave={() => setHover(null)}
        style={{ cursor: "pointer" }}
      >
        {data.speaker_segments.map((segment, index) => {
          const startMs = segment.start_ms;
          const endMs = Math.max(segment.end_ms, segment.start_ms + 1);
          const xStart = (startMs / 1000 / totalSeconds) * width;
          const xWidth = ((endMs - startMs) / 1000 / totalSeconds) * width;
          return (
            <rect
              key={`seg-${index}`}
              x={xStart}
              y={0}
              width={xWidth}
              height={height}
              fill={speakerColors.get(segment.speaker_id) || "var(--mm-rule)"}
              opacity={0.12}
            />
          );
        })}
        {/* Played / unplayed colorisation via clip-paths so the peak <rect>s
            stay static across playhead ticks (audit perf MED). */}
        <defs>
          <clipPath id="mm-wave-played-clip">
            <rect x={0} y={0} width={playheadRatio * width} height={height} />
          </clipPath>
          <clipPath id="mm-wave-unplayed-clip">
            <rect
              x={playheadRatio * width}
              y={0}
              width={Math.max(0, width - playheadRatio * width)}
              height={height}
            />
          </clipPath>
        </defs>
        <WaveformPeaks
          peaks={data.peaks}
          height={height}
          half={half}
          bucketWidth={bucketWidth}
        />
        {/* Playhead */}
        <line
          x1={playheadRatio * width}
          x2={playheadRatio * width}
          y1={0}
          y2={height}
          stroke="var(--mm-clay)"
          strokeWidth={1.5}
          pointerEvents="none"
        />
        {hover && (
          <line
            x1={(hover.seconds / totalSeconds) * width}
            x2={(hover.seconds / totalSeconds) * width}
            y1={0}
            y2={height}
            stroke="var(--mm-sage)"
            strokeOpacity={0.5}
            strokeWidth={1}
            pointerEvents="none"
          />
        )}
      </svg>
      {hover && (
        <div
          className="mm-waveform-hover"
          style={{ left: `${(hover.seconds / totalSeconds) * 100}%` }}
        >
          {formatSeconds(hover.seconds)}
        </div>
      )}
      <div className="mm-waveform-legend">
        {Array.from(speakerSlot.entries()).slice(0, 6).map(([speakerId, slot]) => {
          const label = data.speaker_segments.find((segment) => segment.speaker_id === speakerId)
            ?.label;
          return (
            <span key={speakerId}>
              <i style={{ background: `var(--mm-spk-${slot}-fg)`, opacity: 0.55 }} />
              {label || speakerId}
            </span>
          );
        })}
      </div>
    </div>
  );
}

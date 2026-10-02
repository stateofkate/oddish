"use client";

import { useEffect, useRef, useState } from "react";
import { Button } from "@/components/ui/button";

export function ImageRenderer({
  url,
  fileName,
  fit = false,
}: {
  url: string;
  fileName: string;
  fit?: boolean;
}) {
  const [scale, setScale] = useState<number | null>(null);
  const [dimensions, setDimensions] = useState({ width: 0, height: 0 });
  const area = useRef<HTMLDivElement>(null);
  const [available, setAvailable] = useState({ width: 0, height: 0 });
  useEffect(() => {
    if (!area.current) return;
    const observer = new ResizeObserver(([entry]) => {
      // contentRect excludes the padding around the fitted image.
      setAvailable({ width: entry.contentRect.width, height: entry.contentRect.height });
    });
    observer.observe(area.current);
    return () => observer.disconnect();
  }, []);
  const fitScale = dimensions.width && dimensions.height
    ? Math.min(available.width / dimensions.width, available.height / dimensions.height)
    : 0;
  const displayedScale = scale ?? fitScale;
  const ready = fitScale > 0;
  return (
    <div
      className={`bg-muted/50 flex min-h-0 flex-col ${fit ? "h-full" : "h-[min(600px,70vh)]"}`}
    >
      <div
        className="flex shrink-0 items-center justify-end gap-1 px-2 py-1"
        aria-label="Image zoom"
      >
        <Button
          size="sm"
          variant={scale === null ? "secondary" : "ghost"}
          onClick={() => setScale(null)}
        >
          Fit
        </Button>
        <Button
          size="sm"
          variant={scale === 1 ? "secondary" : "ghost"}
          disabled={!ready}
          onClick={() => setScale(1)}
        >
          100%
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label="Zoom out"
          disabled={!ready || displayedScale <= 0.25}
          onClick={() => setScale(Math.min(4, Math.max(0.25, displayedScale / 1.25)))}
        >
          −
        </Button>
        <Button
          size="sm"
          variant="ghost"
          aria-label="Zoom in"
          disabled={!ready || displayedScale >= 4}
          onClick={() => setScale(Math.max(0.25, Math.min(4, displayedScale * 1.25)))}
        >
          +
        </Button>
        {scale !== null && (
          <span className="w-12 text-right text-xs tabular-nums">
            {Math.round(scale * 100)}%
          </span>
        )}
      </div>
      <div
        ref={area}
        className={`min-h-0 flex-1 p-2 ${scale === null ? "flex overflow-hidden items-center justify-center" : "overflow-auto"}`}
      >
        <img
          src={url}
          alt={fileName}
          onLoad={(event) =>
            setDimensions({
              width: event.currentTarget.naturalWidth,
              height: event.currentTarget.naturalHeight,
            })
          }
          className={
            scale === null ? "h-full w-full object-contain" : "max-w-none"
          }
          style={
            scale !== null && dimensions.width
              ? {
                  width: dimensions.width * scale,
                  height: dimensions.height * scale,
                }
              : undefined
          }
        />
      </div>
    </div>
  );
}

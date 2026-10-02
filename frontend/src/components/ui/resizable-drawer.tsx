"use client";

import * as React from "react";
import { X, GripVertical, Maximize2, Minimize2 } from "lucide-react";
import { cn } from "@/lib/utils";
import { Button } from "@/components/ui/button";

interface ResizableDrawerProps {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  children: React.ReactNode;
  /** Share of the window width (0–1). */
  defaultWidth?: number;
  /** Floor in pixels, so a small share of a small window stays usable. */
  minWidth?: number;
  className?: string;
  /** Hide the close button (useful when parent controls closing) */
  hideCloseButton?: boolean;
  /** Keep drawer mounted in DOM when closed (for smoother transitions) */
  keepMounted?: boolean;
  /** Controlled share of the window width (0–1); overrides internal state. */
  width?: number;
  /** Called with the new share of the window whenever the user drags the edge. */
  onWidthChange?: (width: number) => void;
  maximized?: boolean;
  onMaximizedChange?: (maximized: boolean) => void;
  onResizeEnd?: () => void;
  /** Positions the maximize/close cluster to line up with the child's header. */
  chromeClassName?: string;
}

export function ResizableDrawer({
  open,
  onOpenChange,
  children,
  defaultWidth = 0.45,
  minWidth = 300,
  className,
  hideCloseButton = false,
  width: controlledWidth,
  onWidthChange,
  maximized: controlledMaximized,
  onMaximizedChange,
  onResizeEnd,
  chromeClassName,
}: ResizableDrawerProps) {
  const [internalWidth, setInternalWidth] = React.useState(defaultWidth);
  const width = controlledWidth ?? internalWidth;
  const setWidth = React.useCallback(
    (next: number) => {
      if (controlledWidth === undefined) {
        setInternalWidth(next);
      }
      onWidthChange?.(next);
    },
    [controlledWidth, onWidthChange]
  );
  const [isResizing, setIsResizing] = React.useState(false);
  const drawerRef = React.useRef<HTMLDivElement>(null);

  // Its own state rather than a `width === ceiling` derivation, so the drawer
  // keeps filling the screen when the window is resized and so `width` survives
  // as the restore target.
  const [internalMaximized, setInternalMaximized] = React.useState(false);
  const maximized = controlledMaximized ?? internalMaximized;
  const setMaximized = React.useCallback(
    (next: boolean) => {
      if (controlledMaximized === undefined) setInternalMaximized(next);
      onMaximizedChange?.(next);
    },
    [controlledMaximized, onMaximizedChange]
  );
  // CSS sizes the drawer, so it follows the window from the first paint.
  const cssWidth = maximized
    ? "100vw"
    : `min(100vw, max(${minWidth}px, ${width * 100}vw))`;
  const atFullWidth = maximized || width >= 1;

  // Handle resize via mouse drag
  const handleMouseDown = React.useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      setIsResizing(true);

      const startX = e.clientX;
      const startWidth = drawerRef.current?.getBoundingClientRect().width ?? 0;

      const handleMouseMove = (moveEvent: MouseEvent) => {
        const viewportWidth = window.innerWidth;
        const deltaX = startX - moveEvent.clientX;
        const newWidth = Math.min(
          viewportWidth,
          Math.max(minWidth, startWidth + deltaX)
        );
        const nextMaximized = newWidth >= viewportWidth;
        setMaximized(nextMaximized);
        // Full-screen mode leaves the expanded preference intact for Restore.
        if (!nextMaximized) setWidth(newWidth / viewportWidth);
      };

      const handleMouseUp = () => {
        setIsResizing(false);
        document.removeEventListener("mousemove", handleMouseMove);
        document.removeEventListener("mouseup", handleMouseUp);
        onResizeEnd?.();
      };

      document.addEventListener("mousemove", handleMouseMove);
      document.addEventListener("mouseup", handleMouseUp);
    },
    [minWidth, setMaximized, setWidth, onResizeEnd]
  );

  const toggleMaximized = React.useCallback(() => {
    setMaximized(!maximized);
    onResizeEnd?.();
  }, [maximized, setMaximized, onResizeEnd]);

  // Nested menus/dialogs consume Escape before it reaches this handler.
  React.useEffect(() => {
    const handleKeyDown = (e: KeyboardEvent) => {
      if (e.key === "Escape" && open && !e.defaultPrevented) {
        onOpenChange(false);
      }
    };

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [open, onOpenChange]);

  if (!open) return null;

  return (
    <>
      {/* Backdrop overlay - click to close */}
      <div
        className="animate-in fade-in fixed inset-0 z-30 bg-black/20 duration-300"
        style={{ top: "calc(56px + var(--preview-banner-h, 0px))" }}
        onClick={() => onOpenChange(false)}
      />

      {/* Drawer */}
      <div
        ref={drawerRef}
        data-slot="resizable-drawer"
        className={cn(
          "border-border bg-background fixed right-0 z-40 flex border-l shadow-2xl",
          "animate-in slide-in-from-right duration-300",
          isResizing && "select-none",
          "border-t",
          // At full width the left border sits off-screen and the rounded
          // corner would clip content against the viewport edge.
          atFullWidth ? "border-l-0" : "rounded-tl-lg",
          className
        )}
        style={{
          width: cssWidth,
          // Below the nav header (h-14 = 56px) + optional preview banner
          top: "calc(56px + var(--preview-banner-h, 0px))",
          height: "calc(100vh - 56px - var(--preview-banner-h, 0px))",
        }}
        onClick={(e) => e.stopPropagation()} // Prevent closing when clicking inside drawer
      >
        {/* Resize handle */}
        <div
          className="group hover:bg-primary/20 active:bg-primary/30 absolute top-0 bottom-0 left-0 flex w-1 cursor-ew-resize items-center justify-center"
          onMouseDown={handleMouseDown}
        >
          <div className="bg-muted absolute left-0 flex h-12 w-4 -translate-x-1/2 items-center justify-center rounded-l border border-r-0 opacity-0 transition-opacity group-hover:opacity-100">
            <GripVertical className="text-muted-foreground h-4 w-4" />
          </div>
        </div>

        {/* Top right buttons */}
        <div
          className={cn(
            "absolute top-4 right-4 z-10 flex items-center gap-1",
            chromeClassName
          )}
        >
          {/* Maximize / restore button */}
          <Button
            type="button"
            variant="ghost"
            size="icon"
            onClick={toggleMaximized}
            aria-pressed={maximized}
            title={maximized ? "Restore width" : "Expand to full screen"}
            className="h-8 w-8 opacity-70 hover:opacity-100"
          >
            {maximized ? (
              <Minimize2 className="h-4 w-4" />
            ) : (
              <Maximize2 className="h-4 w-4" />
            )}
            <span className="sr-only">
              {maximized ? "Restore width" : "Expand to full screen"}
            </span>
          </Button>

          {/* Close button */}
          {!hideCloseButton && (
            <Button
              type="button"
              variant="ghost"
              size="icon"
              onClick={() => onOpenChange(false)}
              className="h-8 w-8 opacity-70 hover:opacity-100"
            >
              <X className="h-4 w-4" />
              <span className="sr-only">Close</span>
            </Button>
          )}
        </div>

        {/* Content */}
        <div className="flex flex-1 flex-col overflow-hidden">{children}</div>
      </div>
    </>
  );
}

// Sub-components for consistent structure
export function DrawerHeader({
  className,
  ...props
}: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex flex-col space-y-2 text-left", className)}
      {...props}
    />
  );
}

export function DrawerTitle({
  className,
  ...props
}: React.HTMLAttributes<HTMLHeadingElement>) {
  return (
    <h2
      className={cn("text-foreground text-lg font-semibold", className)}
      {...props}
    />
  );
}

export function DrawerDescription({
  className,
  ...props
}: React.HTMLAttributes<HTMLParagraphElement>) {
  return (
    <p
      className={cn("text-muted-foreground sr-only text-sm", className)}
      {...props}
    />
  );
}

"use client";

/** The PDF path of the attachment preview overlay: every page laid out in
 * one scrolling column, each page's own bytes drawn to canvas only once it
 * approaches the viewport -- never all up front, and never a full
 * virtualization list, since a mail attachment is usually a handful of
 * pages. A page's exact size is known from pdf.js's own metadata the
 * moment it is fetched, before any pixel of it is ever drawn, so its
 * column slot never carries a guessed height that later has to be
 * corrected -- there is nothing here for the reader to be dragged around
 * by.
 *
 * The worker is served as a real same-origin file (copied from the
 * installed package at build time, see scripts/copy-pdf-worker.mjs) rather
 * than reached through pdf.js's blob-based fallback worker, which needs a
 * CSP grant this application does not make. Embedded PDF scripting stays
 * off by pdf.js's own default, so a hostile PDF's worst case is a parse
 * error, handled the same way as a corrupt image. */

import { useEffect, useRef, useState } from "react";
import type { PDFDocumentProxy, PDFPageProxy } from "pdfjs-dist";
import { ZoomIn, ZoomOut } from "lucide-react";
import { Button } from "@/components/ui/button";
import { PreviewUnavailable } from "@/components/mail/preview-unavailable";

/** Loaded lazily, and only in the browser -- a static import runs pdf.js's
 * top-level module code during the static export's own Node-side prerender
 * pass too, where it warns about being outside a browser. Type-only
 * imports above are erased at compile time and never trigger that. It also
 * keeps the library, a sizeable one, out of every page's initial bundle
 * for a reader who never opens a PDF attachment. */
type PdfjsModule = typeof import("pdfjs-dist");

const MIN_ZOOM = 0.5;
const MAX_ZOOM = 3;
const ZOOM_STEP = 0.25;

// MAX_PREVIEW_BYTES bounds the file this preview ever opens, not how many
// pages are inside it -- a PDF a few MB in size can still carry many
// thousands of pages, each with its own mounted slot and
// IntersectionObserver even with the draw (and now the fetch) gated on
// proximity. This is a hard ceiling on the ordinary case ("a mail
// attachment is usually a handful of pages") rather than a bound this
// component tries to hold under, since a stranger's PDF is exactly the
// kind of file that would otherwise be trusted to stay small.
const MAX_RENDERED_PAGES = 300;

/** How far past the visible area a page still gets drawn -- a couple of
 * screens' worth of the scroll container's own measured height, in pixels
 * rather than a percentage: percentage rootMargin resolves against the
 * root's width on every side, top and bottom included. */
function nearMargin(containerHeight: number): string {
  const px = Math.round(containerHeight * 2);
  return `${px}px 0px`;
}

function PdfPage({
  doc,
  pageNumber,
  columnWidth,
  zoom,
  scrollRoot,
  estimatedAspectRatio,
}: {
  doc: PDFDocumentProxy;
  pageNumber: number;
  columnWidth: number;
  zoom: number;
  scrollRoot: Element | null;
  // height/width of page 1, fetched once for the whole document -- what
  // sizes this page's slot before its own metadata has loaded, so the
  // column never collapses a not-yet-near page to zero height (which
  // would defeat the IntersectionObserver measuring against it, and
  // jump the scroll position once the real size lands).
  estimatedAspectRatio: number;
}) {
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [isNear, setIsNear] = useState(false);
  const [failed, setFailed] = useState(false);
  const containerRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const lastRenderedZoomRef = useRef<number | null>(null);
  const renderTaskRef = useRef<ReturnType<PDFPageProxy["render"]> | null>(null);

  // Fetching the page object itself -- not only drawing it -- waits for
  // isNear too. doc.getPage() holds the parsed page dictionary and its
  // resources in memory for as long as anything references it, so
  // fetching every page up front is the same unbounded-memory shape the
  // draw-gating below already avoids for canvases: a PDF bounded only by
  // MAX_PREVIEW_BYTES can still carry many thousands of pages.
  useEffect(() => {
    if (!isNear) return;
    let cancelled = false;
    doc.getPage(pageNumber).then(
      (p) => {
        if (!cancelled) setPage(p);
      },
      () => {
        if (!cancelled) setFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [doc, pageNumber, isNear]);

  useEffect(() => {
    const el = containerRef.current;
    if (!el || !scrollRoot) return;
    const rootHeight = scrollRoot.getBoundingClientRect().height || 800;
    const observer = new IntersectionObserver(
      (entries) => {
        const entry = entries[0];
        if (entry) setIsNear(entry.isIntersecting);
      },
      { root: scrollRoot, rootMargin: nearMargin(rootHeight) },
    );
    observer.observe(el);
    return () => observer.disconnect();
  }, [scrollRoot]);

  const naturalViewport = page?.getViewport({ scale: 1 });
  const scale = naturalViewport && columnWidth > 0 ? (columnWidth / naturalViewport.width) * zoom : 0;
  const scaledWidth = naturalViewport ? naturalViewport.width * scale : columnWidth;
  const scaledHeight = naturalViewport
    ? naturalViewport.height * scale
    : columnWidth * estimatedAspectRatio * zoom;

  useEffect(() => {
    if (!page || !isNear || scale <= 0 || failed) return;
    if (lastRenderedZoomRef.current === scale) return;
    const canvas = canvasRef.current;
    if (!canvas) return;
    let cancelled = false;

    (async () => {
      // pdf.js refuses a second render() on the same canvas while one is
      // still in flight -- a zoom change while the previous draw is
      // running has to wait out its cancellation before starting the
      // next one, not merely call cancel() and move on.
      const previous = renderTaskRef.current;
      if (previous) {
        previous.cancel();
        await previous.promise.catch(() => {});
      }
      if (cancelled) return;

      const dpr = window.devicePixelRatio || 1;
      const viewport = page.getViewport({ scale: scale * dpr });
      canvas.width = viewport.width;
      canvas.height = viewport.height;
      canvas.style.width = `${scaledWidth}px`;
      canvas.style.height = `${scaledHeight}px`;

      const task = page.render({ canvas, viewport });
      renderTaskRef.current = task;
      lastRenderedZoomRef.current = scale;
      try {
        await task.promise;
      } catch {
        // A cancelled render (superseded by a later zoom change) rejects
        // too -- only a real decode failure should show the fallback.
        if (!cancelled && task === renderTaskRef.current) setFailed(true);
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [page, isNear, scale, scaledWidth, scaledHeight, failed]);

  if (failed) {
    return (
      <div
        ref={containerRef}
        style={{ width: columnWidth, minHeight: 200 }}
        className="mx-auto mb-2 flex items-center justify-center rounded border bg-muted/30 text-xs text-muted-foreground"
      >
        Page {pageNumber} could not be rendered.
      </div>
    );
  }

  return (
    <div
      ref={containerRef}
      data-testid="pdf-page"
      data-page-number={pageNumber}
      style={{ width: scaledWidth || columnWidth, height: scaledHeight || undefined }}
      className="mx-auto mb-2 shadow-sm"
    >
      {page ? <canvas ref={canvasRef} className="block" /> : null}
    </div>
  );
}

export function AttachmentPdfPreview({ src }: { src: string }) {
  const [pdfjs, setPdfjs] = useState<PdfjsModule | null>(null);
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  // Page 1's own aspect ratio, fetched once for the whole document -- the
  // one page every PdfPage slot needs a real size estimate from before
  // its own turn to load, not fetched per page (that would be exactly
  // the up-front cost this component exists to avoid).
  const [estimatedAspectRatio, setEstimatedAspectRatio] = useState<number | null>(null);
  const [failed, setFailed] = useState(false);
  const [zoom, setZoom] = useState(1);
  const [columnWidth, setColumnWidth] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    let cancelled = false;
    import("pdfjs-dist").then(
      (mod) => {
        if (cancelled) return;
        mod.GlobalWorkerOptions.workerSrc = "/pdf.worker.min.mjs";
        setPdfjs(mod);
      },
      () => {
        if (!cancelled) setFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, []);

  useEffect(() => {
    if (!pdfjs) return;
    let cancelled = false;
    const loadingTask = pdfjs.getDocument({ url: src });
    loadingTask.promise.then(
      (d) => {
        if (!cancelled) setDoc(d);
      },
      () => {
        if (!cancelled) setFailed(true);
      },
    );
    return () => {
      cancelled = true;
      loadingTask.destroy();
    };
  }, [pdfjs, src]);

  useEffect(() => {
    if (!doc) return;
    let cancelled = false;
    doc.getPage(1).then((p) => {
      if (cancelled) return;
      const v = p.getViewport({ scale: 1 });
      setEstimatedAspectRatio(v.height / v.width);
    }, () => {
      // Page 1 itself failed to load -- every other page's own getPage()
      // will fail the same way once it becomes near and reports its own
      // per-page failure there; a fallback ratio just keeps not-yet-near
      // slots from collapsing to zero height in the meantime.
      if (!cancelled) setEstimatedAspectRatio(1.4);
    });
    return () => {
      cancelled = true;
    };
  }, [doc]);

  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const measure = () => setColumnWidth(el.clientWidth - 32);
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  if (failed) {
    return <PreviewUnavailable url={src} filename="attachment.pdf" reason="This file could not be previewed." />;
  }

  const renderedPageCount = doc ? Math.min(doc.numPages, MAX_RENDERED_PAGES) : 0;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div
        ref={scrollRef}
        // The compensation this repo's own scrolling guidance calls for
        // has nothing to correct here -- every page's slot is sized from
        // real pdf.js metadata before it is ever laid out, so its height
        // never changes after the fact. Set anyway, since one mechanism
        // that always runs (or in this case, is simply never needed) beats
        // relying on a browser default that varies by engine.
        style={{ overflowAnchor: "none" }}
        className="min-h-0 flex-1 overflow-y-auto p-4"
      >
        {(!doc || estimatedAspectRatio === null) && (
          <p className="p-6 text-center text-sm text-muted-foreground">Loading…</p>
        )}
        {doc &&
          estimatedAspectRatio !== null &&
          columnWidth > 0 &&
          Array.from({ length: renderedPageCount }, (_, i) => i + 1).map((pageNumber) => (
            <PdfPage
              key={pageNumber}
              doc={doc}
              pageNumber={pageNumber}
              columnWidth={columnWidth}
              zoom={zoom}
              scrollRoot={scrollRef.current}
              estimatedAspectRatio={estimatedAspectRatio}
            />
          ))}
        {doc && doc.numPages > MAX_RENDERED_PAGES && (
          <p className="p-4 text-center text-xs text-muted-foreground">
            Showing the first {MAX_RENDERED_PAGES} of {doc.numPages} pages. Download the file to
            see the rest.
          </p>
        )}
      </div>
      <div className="flex shrink-0 items-center justify-center gap-2 border-t p-2">
        <Button
          variant="outline"
          size="icon-sm"
          onClick={() => setZoom((z) => Math.max(MIN_ZOOM, z - ZOOM_STEP))}
          title="Zoom out"
          aria-label="Zoom out"
        >
          <ZoomOut className="h-3.5 w-3.5" />
        </Button>
        <span className="w-12 text-center text-xs tabular-nums text-muted-foreground">
          {Math.round(zoom * 100)}%
        </span>
        <Button
          variant="outline"
          size="icon-sm"
          onClick={() => setZoom((z) => Math.min(MAX_ZOOM, z + ZOOM_STEP))}
          title="Zoom in"
          aria-label="Zoom in"
        >
          <ZoomIn className="h-3.5 w-3.5" />
        </Button>
        <Button variant="outline" size="sm" onClick={() => setZoom(1)}>
          Reset
        </Button>
      </div>
    </div>
  );
}

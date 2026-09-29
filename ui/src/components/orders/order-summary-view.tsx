import { parseOrderSummary } from "@/lib/order-summary";

/** Renders the markdown subset orders/text.py's write call answers with --
 * paragraphs, a bullet list, **bold** -- never dangerouslySetInnerHTML. */
export function OrderSummaryView({ text }: { text: string }) {
  const blocks = parseOrderSummary(text);
  return (
    <div className="space-y-3">
      {blocks.map((block, i) =>
        block.kind === "paragraph" ? (
          <p key={i}>
            {block.inlines.map((inline, j) =>
              inline.bold ? (
                <span key={j} className="font-semibold">
                  {inline.text}
                </span>
              ) : (
                <span key={j}>{inline.text}</span>
              ),
            )}
          </p>
        ) : (
          <ul key={i} className="list-disc pl-5 space-y-1">
            {block.items.map((item, j) => (
              <li key={j}>
                {item.map((inline, k) =>
                  inline.bold ? (
                    <span key={k} className="font-semibold">
                      {inline.text}
                    </span>
                  ) : (
                    <span key={k}>{inline.text}</span>
                  ),
                )}
              </li>
            ))}
          </ul>
        ),
      )}
    </div>
  );
}

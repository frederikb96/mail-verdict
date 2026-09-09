"use client";

import { Slider as SliderPrimitive } from "@base-ui/react/slider";

import { cn } from "@/lib/utils";

interface SliderProps<Value extends number | readonly number[]>
  extends React.ComponentPropsWithoutRef<typeof SliderPrimitive.Root<Value>> {
  className?: string;
  /** One thumb, or one per entry for a range slider (two, here). */
  thumbCount?: number;
}

function Slider<Value extends number | readonly number[]>({
  className,
  thumbCount = 1,
  ...props
}: SliderProps<Value>) {
  return (
    <SliderPrimitive.Root className={cn("relative flex w-full touch-none select-none items-center", className)} {...props}>
      <SliderPrimitive.Control className="relative flex w-full items-center py-2">
        <SliderPrimitive.Track className="relative h-1.5 w-full grow rounded-full bg-muted">
          <SliderPrimitive.Indicator className="absolute h-full rounded-full bg-primary" />
          {Array.from({ length: thumbCount }).map((_, i) => (
            <SliderPrimitive.Thumb
              key={i}
              index={i}
              className={cn(
                "block h-4 w-4 rounded-full border-2 border-primary bg-background shadow",
                "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 focus-visible:ring-offset-background",
              )}
            />
          ))}
        </SliderPrimitive.Track>
      </SliderPrimitive.Control>
    </SliderPrimitive.Root>
  );
}

export { Slider };

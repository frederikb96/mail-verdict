import { Suspense } from "react";
import { ClientOnly } from "@/components/client-only";
import { OrdersPage } from "@/components/orders/orders-page";

export default function OrdersRoute() {
  return (
    <ClientOnly>
      <Suspense fallback={null}>
        <OrdersPage />
      </Suspense>
    </ClientOnly>
  );
}

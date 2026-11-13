import { AdminOnly } from "@/components/common/admin-only";
import { PipelinePage } from "@/components/pipeline/pipeline-page";

export default function PipelineRoute() {
  return (
    <AdminOnly>
      <PipelinePage />
    </AdminOnly>
  );
}

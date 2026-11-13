import { PipelinePage } from "@/components/pipeline/pipeline-page";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function PipelineRoute() {
  return (
    <SettingsShell area="admin" category="processing">
      <PipelinePage />
    </SettingsShell>
  );
}

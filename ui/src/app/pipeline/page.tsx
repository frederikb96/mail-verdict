import { ProcessingPage } from "@/components/pipeline/processing-page";
import { SettingsShell } from "@/components/settings/settings-shell";

export default function PipelineRoute() {
  return (
    <SettingsShell area="admin" category="processing">
      <ProcessingPage />
    </SettingsShell>
  );
}

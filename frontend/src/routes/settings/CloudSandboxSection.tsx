import { useState } from "react";
import { useQueryClient } from "@tanstack/react-query";
import { Input, Field } from "@/ui/Input";
import { Switch } from "@/ui/Switch";
import { toast } from "@/ui/Toast";
import { ApiError } from "@/api/client";
import { configApi, useConfig } from "@/api/config";
import { qk } from "@/api/keys";
import type { ConfigOut } from "@/api/types";
import { SectionHeader, SettingsCard } from "./parts/SettingsSection";
import { SaveBar, useEditableForm } from "./parts/SaveBar";

interface K8sForm {
  k8s_runner_image: string;
  k8s_namespace: string;
  k8s_kubeconfig_path: string;
  k8s_in_cluster: boolean;
  k8s_cpu_request: string;
  k8s_cpu_limit: string;
  k8s_mem_request: string;
  k8s_mem_limit: string;
  k8s_runtime_class: string;
  k8s_git_credentials_secret: string;
}

function mapForm(c: ConfigOut): K8sForm {
  return {
    k8s_runner_image: c.k8s_runner_image ?? "",
    k8s_namespace: c.k8s_namespace ?? "nightdesk",
    k8s_kubeconfig_path: c.k8s_kubeconfig_path ?? "",
    k8s_in_cluster: !!c.k8s_in_cluster,
    k8s_cpu_request: c.k8s_cpu_request ?? "",
    k8s_cpu_limit: c.k8s_cpu_limit ?? "",
    k8s_mem_request: c.k8s_mem_request ?? "",
    k8s_mem_limit: c.k8s_mem_limit ?? "",
    k8s_runtime_class: c.k8s_runtime_class ?? "",
    k8s_git_credentials_secret: c.k8s_git_credentials_secret ?? "",
  };
}

export function CloudSandboxSection() {
  const config = useConfig();
  const qc = useQueryClient();
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const { form, setForm, dirty, discard, commit } = useEditableForm<ConfigOut, K8sForm>(
    config.data,
    mapForm,
    config.data ? JSON.stringify(mapForm(config.data)) : "loading",
  );

  async function save() {
    if (!form) return;
    setSaving(true);
    setError(null);
    try {
      await configApi.update({
        k8s_runner_image: form.k8s_runner_image.trim(),
        k8s_namespace: form.k8s_namespace.trim() || "nightdesk",
        k8s_kubeconfig_path: form.k8s_kubeconfig_path.trim(),
        k8s_in_cluster: form.k8s_in_cluster,
        k8s_cpu_request: form.k8s_cpu_request.trim(),
        k8s_cpu_limit: form.k8s_cpu_limit.trim(),
        k8s_mem_request: form.k8s_mem_request.trim(),
        k8s_mem_limit: form.k8s_mem_limit.trim(),
        k8s_runtime_class: form.k8s_runtime_class.trim(),
        k8s_git_credentials_secret: form.k8s_git_credentials_secret.trim(),
      });
      await qc.invalidateQueries({ queryKey: qk.config });
      commit();
      toast.success("Cloud sandbox settings saved");
    } catch (err) {
      const msg = err instanceof ApiError ? err.message : "Could not save";
      setError(msg);
      toast.error(msg);
    } finally {
      setSaving(false);
    }
  }

  return (
    <div>
      <SectionHeader
        title="Cloud sandbox (Kubernetes)"
        description="Run a profile's tickets in a per-run Kubernetes pod instead of the on-host sandbox. Set a runner image here, then switch a profile's execution target to Kubernetes. Requires an API address the cluster can reach — 127.0.0.1 is unreachable from a pod."
      />

      <div className="space-y-5">
        <SettingsCard
          title="Cluster connection"
          description="How the worker reaches the cluster and where runner pods land."
        >
          <div className="space-y-4">
            <Field
              label="Runner image"
              hint="Container image built from docker/runner/Dockerfile. Required to run any k8s ticket."
            >
              <Input
                mono
                placeholder="ghcr.io/your-org/nightdesk-runner:latest"
                value={form?.k8s_runner_image ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_runner_image: e.target.value }))}
                disabled={!form}
              />
            </Field>
            <Field label="Namespace" hint="Pods and per-run Secrets are created here.">
              <Input
                mono
                placeholder="nightdesk"
                value={form?.k8s_namespace ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_namespace: e.target.value }))}
                disabled={!form}
              />
            </Field>
            <div className="flex items-center justify-between gap-4 rounded-card border border-ink-700 bg-ink-950/40 px-3.5 py-2.5">
              <div>
                <p className="text-sm text-moon-100">In-cluster config</p>
                <p className="text-xs text-moon-400">
                  On when the worker itself runs inside the cluster (uses the pod service account).
                </p>
              </div>
              <Switch
                checked={!!form?.k8s_in_cluster}
                onChange={(v) => setForm((f) => ({ ...f, k8s_in_cluster: v }))}
                disabled={!form}
                aria-label="Use in-cluster config"
              />
            </div>
            <Field
              label="Kubeconfig path"
              hint="Out-of-cluster only. Empty uses the default kubeconfig resolution."
            >
              <Input
                mono
                placeholder="~/.kube/config"
                value={form?.k8s_kubeconfig_path ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_kubeconfig_path: e.target.value }))}
                disabled={!form || !!form?.k8s_in_cluster}
              />
            </Field>
            <Field
              label="Git credentials Secret"
              hint="Name of a pre-existing cluster Secret (HTTPS token or deploy key) mounted for clone/push."
            >
              <Input
                mono
                placeholder="(optional)"
                value={form?.k8s_git_credentials_secret ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_git_credentials_secret: e.target.value }))}
                disabled={!form}
              />
            </Field>
          </div>
        </SettingsCard>

        <SettingsCard
          title="Pod shape"
          description="Resource requests/limits and isolation for each runner pod. Leave blank to let the cluster default apply."
        >
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
            <Field label="CPU request">
              <Input mono placeholder="500m" value={form?.k8s_cpu_request ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_cpu_request: e.target.value }))}
                disabled={!form} />
            </Field>
            <Field label="CPU limit">
              <Input mono placeholder="2" value={form?.k8s_cpu_limit ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_cpu_limit: e.target.value }))}
                disabled={!form} />
            </Field>
            <Field label="Memory request">
              <Input mono placeholder="1Gi" value={form?.k8s_mem_request ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_mem_request: e.target.value }))}
                disabled={!form} />
            </Field>
            <Field label="Memory limit">
              <Input mono placeholder="4Gi" value={form?.k8s_mem_limit ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_mem_limit: e.target.value }))}
                disabled={!form} />
            </Field>
            <Field
              label="Runtime class"
              hint="Optional sandboxed runtime (e.g. gvisor, kata)."
            >
              <Input mono placeholder="(cluster default)" value={form?.k8s_runtime_class ?? ""}
                onChange={(e) => setForm((f) => ({ ...f, k8s_runtime_class: e.target.value }))}
                disabled={!form} />
            </Field>
          </div>
        </SettingsCard>
      </div>

      <SaveBar dirty={dirty} saving={saving} onSave={save} onDiscard={discard} error={error} />
    </div>
  );
}

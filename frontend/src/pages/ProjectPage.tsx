import { ArrowLeft, Network, Sparkles } from "lucide-react";
import { Link, useParams } from "wouter";
import { useCreateModel, useModels, useProject } from "@/api/hooks";
import { InlineCreate } from "@/components/InlineCreate";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { SourcesManager } from "@/components/sources/SourcesManager";

export default function ProjectPage() {
  const { tenantId, projectId } = useParams<{ tenantId: string; projectId: string }>();
  const path = { tenant_id: tenantId, project_id: projectId };
  const project = useProject(path);
  const models = useModels(path);
  const createModel = useCreateModel(path);
  const canEdit = project.data && project.data.role !== "viewer";

  if (project.error) return <p className="text-muted-foreground">Project not found.</p>;

  return (
    <div className="space-y-6">
      <Link href="/" className="inline-flex items-center gap-1 text-sm text-muted-foreground hover:text-foreground">
        <ArrowLeft className="size-4" /> Workspaces
      </Link>
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold flex items-center gap-3">
            {project.data?.name ?? "…"}
            {project.data && <Badge variant="secondary">{project.data.role}</Badge>}
          </h1>
          {project.data?.description && <p className="text-muted-foreground text-sm">{project.data.description}</p>}
        </div>
        {canEdit && (
          <div className="flex gap-2 items-start">
          <Link href={`/t/${tenantId}/p/${projectId}/new-model`}>
            <Button size="sm"><Sparkles className="size-4" /> New model (guided)</Button>
          </Link>
          <InlineCreate
            label="Quick create"
            placeholder="Model name"
            pending={createModel.isPending}
            onCreate={(name) => createModel.mutateAsync({ name })}
          />
          </div>
        )}
      </div>

      {models.data?.length === 0 && <p className="text-muted-foreground">No data models yet.</p>}
      <div className="grid gap-4 md:grid-cols-3">
        {models.data?.map((m) => (
          <Link key={m.id} href={`/t/${tenantId}/p/${projectId}/m/${m.id}`}>
            <Card className="hover:border-primary transition-colors">
              <CardHeader className="flex flex-row items-center gap-2">
                <Network className="size-5 text-muted-foreground" />
                <CardTitle className="text-base">{m.name}</CardTitle>
              </CardHeader>
              {m.description && <CardContent className="text-sm text-muted-foreground">{m.description}</CardContent>}
            </Card>
          </Link>
        ))}
      </div>

      <section className="space-y-3 pt-4">
        <h2 className="text-lg font-semibold">Data sources</h2>
        <SourcesManager path={path} role={project.data?.role} />
      </section>
    </div>
  );
}

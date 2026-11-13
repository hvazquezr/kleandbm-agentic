import { Building2, FolderKanban } from "lucide-react";
import { Link } from "wouter";
import { useCreateProject, useCreateTenant, useProjects, useTenants } from "@/api/hooks";
import { InlineCreate } from "@/components/InlineCreate";
import { Badge } from "@/components/ui/badge";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";

export default function Home() {
  const tenants = useTenants();
  const createTenant = useCreateTenant();

  return (
    <div className="space-y-6">
      <div className="flex items-end justify-between gap-4">
        <div>
          <h1 className="text-2xl font-semibold">Workspaces</h1>
          <p className="text-muted-foreground text-sm">Each workspace is a separate tenant with its own projects.</p>
        </div>
        <InlineCreate
          label="New workspace"
          placeholder="Workspace name"
          pending={createTenant.isPending}
          onCreate={(name) => createTenant.mutateAsync(name)}
        />
      </div>

      {tenants.isLoading && <p className="text-muted-foreground">Loading…</p>}
      {tenants.data?.length === 0 && (
        <p className="text-muted-foreground">You aren't a member of any workspace yet. Create one to start.</p>
      )}
      <div className="grid gap-4 md:grid-cols-2">
        {tenants.data?.map((t) => (
          <TenantCard key={t.id} tenantId={t.id} name={t.name} role={t.role} />
        ))}
      </div>
    </div>
  );
}

function TenantCard({ tenantId, name, role }: { tenantId: string; name: string; role: string }) {
  const projects = useProjects(tenantId);
  const createProject = useCreateProject(tenantId);

  return (
    <Card>
      <CardHeader className="flex flex-row items-center gap-2">
        <Building2 className="size-5 text-muted-foreground" />
        <CardTitle className="text-lg">{name}</CardTitle>
        <Badge variant="outline" className="ml-auto">{role}</Badge>
      </CardHeader>
      <CardContent className="space-y-3">
        {projects.data?.length === 0 && <p className="text-sm text-muted-foreground">No projects you can access.</p>}
        <ul className="space-y-1">
          {projects.data?.map((p) => (
            <li key={p.id}>
              <Link
                href={`/t/${tenantId}/p/${p.id}`}
                className="flex items-center gap-2 rounded-md px-2 py-1.5 hover:bg-accent"
              >
                <FolderKanban className="size-4 text-muted-foreground" />
                <span className="font-medium">{p.name}</span>
                <Badge variant="secondary" className="ml-auto">{p.role}</Badge>
              </Link>
            </li>
          ))}
        </ul>
        <InlineCreate
          label="New project"
          placeholder="Project name"
          pending={createProject.isPending}
          onCreate={(n) => createProject.mutateAsync({ name: n })}
        />
      </CardContent>
    </Card>
  );
}

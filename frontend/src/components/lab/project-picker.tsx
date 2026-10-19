"use client";

import { Avatar, AvatarFallback } from "@midday/ui/avatar";
import { cn } from "@midday/ui/cn";
import { Icons } from "@midday/ui/icons";
import { Popover, PopoverContent, PopoverTrigger } from "@midday/ui/popover";
import {
  Tooltip,
  TooltipContent,
  TooltipProvider,
  TooltipTrigger,
} from "@midday/ui/tooltip";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { deleteProject, fetchProjects } from "./speechcraft-api";

type ProjectPickerProps = {
  variant?: "sidebar" | "header";
};

export function ProjectPicker({ variant = "sidebar" }: ProjectPickerProps) {
  const router = useRouter();
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const queryClient = useQueryClient();

  const { data: projects = [] } = useQuery({
    queryKey: ["sc-projects"],
    queryFn: fetchProjects,
    staleTime: 60_000,
  });

  const paramId = searchParams.get("project");
  const activeProject =
    projects.find((p) => p.id === paramId) ?? projects[0] ?? null;

  const selectProject = (id: string) => {
    const params = new URLSearchParams(searchParams.toString());
    params.set("project", id);
    params.delete("run");
    router.replace(`${pathname}?${params.toString()}`);
  };

  const removeProject = async (project: { id: string; name: string }) => {
    const confirmed = window.confirm(`Delete “${project.name}” and its uploaded copies, processing runs, and exports? The source WAV outside Speechcraft storage will be preserved.`);
    if (!confirmed) return;
    try {
      await deleteProject(project.id);
      await queryClient.invalidateQueries({ queryKey: ["sc-projects"] });
      if (project.id === activeProject?.id) router.replace("/lab");
    } catch (error) {
      window.alert(error instanceof Error ? error.message : "Project deletion failed.");
    }
  };

  const trigger = (
    <PopoverTrigger asChild>
      <button
        type="button"
        aria-label={`Switch project (current: ${activeProject?.name ?? "No project"})`}
        className={cn(
          "flex h-8 items-center justify-center px-2 text-sm text-foreground hover:bg-secondary",
          variant === "header" && "max-w-48 min-w-0",
        )}
      >
        {variant === "header" ? (
          <span className="truncate">{activeProject?.name ?? "No project"}</span>
        ) : (
          "Projects"
        )}
      </button>
    </PopoverTrigger>
  );

  return (
    <TooltipProvider delayDuration={50}>
      <Popover>
        {variant === "header" ? (
          trigger
        ) : (
          <Tooltip>
            <TooltipTrigger asChild>{trigger}</TooltipTrigger>
            <TooltipContent side="bottom" sideOffset={8}>
              <p className="text-xs">{activeProject?.name ?? "No project"}</p>
            </TooltipContent>
          </Tooltip>
        )}

        <PopoverContent side="bottom" align="end" className="w-56 p-1">
          <p className="px-2 py-1.5 text-[10px] font-medium uppercase tracking-wide text-muted-foreground">
            Project
          </p>
          <div className="max-h-[min(70vh,24rem)] overflow-y-auto">
            {projects.map((project) => {
              const isActive = project.id === activeProject?.id;
              return (
                <div
                  key={project.id}
                  className={cn("flex w-full items-center gap-1 rounded-sm px-1 hover:bg-secondary", isActive && "text-foreground")}
                >
                  <button type="button" onClick={() => selectProject(project.id)} title={project.name}
                    className="flex min-w-0 flex-1 items-center justify-between gap-2 py-1.5 text-left text-sm">
                    <span className="flex min-w-0 items-center gap-2">
                      <Avatar className="h-5 w-5 shrink-0 rounded-none border border-border">
                        <AvatarFallback className="rounded-none text-[9px]">{project.name.slice(0, 2).toUpperCase()}</AvatarFallback>
                      </Avatar>
                      <span className="truncate">{project.name}</span>
                    </span>
                    {isActive ? <Icons.Check className="size-4 shrink-0" /> : null}
                  </button>
                  <button type="button" onClick={() => void removeProject(project)}
                    aria-label={`Delete project ${project.name}`} title={`Delete ${project.name}`}
                    className="shrink-0 px-2 py-1 text-xs text-muted-foreground hover:text-destructive">Delete</button>
                </div>
              );
            })}
          </div>
        </PopoverContent>
      </Popover>
    </TooltipProvider>
  );
}

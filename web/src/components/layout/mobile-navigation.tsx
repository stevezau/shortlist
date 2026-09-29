import { Menu } from "lucide-react";
import { useState, type ReactNode } from "react";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";

/** Modal navigation shares the dialog's focus trap, Escape handling and trigger restoration. */
export function MobileNavigation({ children }: { children: ReactNode }) {
  const [open, setOpen] = useState(false);
  return <Dialog open={open} onOpenChange={setOpen}>
    <DialogTrigger asChild>
      <Button variant="ghost" size="icon" aria-label="Open menu"><Menu aria-hidden="true" /></Button>
    </DialogTrigger>
    <DialogContent
      aria-describedby={undefined}
      className="inset-y-0 left-0 top-0 flex h-dvh w-72 max-w-[85vw] translate-x-0 translate-y-0 flex-col gap-0 rounded-none bg-card p-0 sm:rounded-none"
      onClick={(event) => { if ((event.target as HTMLElement).closest("a")) setOpen(false); }}
    >
      <DialogTitle className="border-b px-5 py-5 text-primary">Main menu</DialogTitle>
      {children}
    </DialogContent>
  </Dialog>;
}

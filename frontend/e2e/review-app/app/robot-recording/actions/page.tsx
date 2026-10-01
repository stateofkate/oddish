"use client";

import { useState } from "react";
import { ResizableDrawer, DrawerTitle } from "@/components/ui/resizable-drawer";
import { DropdownMenu, DropdownMenuTrigger, DropdownMenuContent, DropdownMenuItem } from "@/components/ui/dropdown-menu";

export default function DrawerActionsFixture() {
  const [open, setOpen] = useState(true);
  return <ResizableDrawer open={open} onOpenChange={setOpen}>
    <DrawerTitle>Robot pick-and-place</DrawerTitle>
    <DropdownMenu>
      <DropdownMenuTrigger>Trial actions</DropdownMenuTrigger>
      <DropdownMenuContent>
        <DropdownMenuItem disabled>Retry trial</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  </ResizableDrawer>;
}

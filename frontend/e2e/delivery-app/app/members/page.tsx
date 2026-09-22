"use client";
import { useState } from "react";
import { MemberFilterTypeahead } from "../../../../src/components/member-filter-typeahead";
export default function MembersFixture() {
  const [selected, setSelected] = useState<string | null>(null);
  return (
    <>
      <MemberFilterTypeahead selectedUserId={selected} onSelect={setSelected} />
      <output>{selected}</output>
    </>
  );
}

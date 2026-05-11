import { api } from "./api";

export type OrgRole = "owner" | "admin" | "member";

export type Org = {
  id: number;
  name: string;
  slug: string;
  role: OrgRole;
};

export type Member = {
  id: number;
  username: string;
  display_name: string;
  email: string | null;
  role: OrgRole;
  joined_at: string;
};

export type MyOrgsResponse = { orgs: Org[]; current: Org };

export function fetchMyOrgs(): Promise<MyOrgsResponse> {
  return api<MyOrgsResponse>("/api/orgs/me");
}

export function switchOrg(org_id: number): Promise<{ current_org_id: number }> {
  return api<{ current_org_id: number }>("/api/me/current-org", {
    method: "POST",
    body: JSON.stringify({ org_id }),
  });
}

export function fetchMembers(org_id: number): Promise<{ members: Member[] }> {
  return api<{ members: Member[] }>(`/api/orgs/${org_id}/members`);
}

export function inviteMember(
  org_id: number,
  email: string,
  role: "admin" | "member",
): Promise<{ user_id: number; role: string }> {
  return api(`/api/orgs/${org_id}/members`, {
    method: "POST",
    body: JSON.stringify({ email, role }),
  });
}

export function removeMember(org_id: number, user_id: number): Promise<{ removed: boolean }> {
  return api(`/api/orgs/${org_id}/members/${user_id}`, { method: "DELETE" });
}

export function changeRole(
  org_id: number,
  user_id: number,
  role: "admin" | "member",
): Promise<{ user_id: number; role: string }> {
  return api(`/api/orgs/${org_id}/members/${user_id}`, {
    method: "PATCH",
    body: JSON.stringify({ role }),
  });
}

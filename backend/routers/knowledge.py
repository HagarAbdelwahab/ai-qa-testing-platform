"""Knowledge Base CRUD — /api/knowledge"""
from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from typing import Literal, Optional

import db

router = APIRouter()


class KBEntry(BaseModel):
    title: str
    content: str
    tags: str = ""


class LinkEntry(BaseModel):
    title: str
    url: str = ""
    description: str = ""
    source_type: Literal["postman", "confluence"]
    sub_section: str = ""
    collection_json: str = ""


@router.get("")
def list_entries():
    return db.kb_list()


@router.post("", status_code=201)
def create_entry(body: KBEntry):
    return db.kb_create(body.title, body.content, body.tags)


@router.put("/{entry_id}")
def update_entry(entry_id: int, body: KBEntry):
    entry = db.kb_update(entry_id, body.title, body.content, body.tags)
    if not entry:
        raise HTTPException(404, "Entry not found")
    return entry


@router.delete("/{entry_id}", status_code=204)
def delete_entry(entry_id: int):
    if not db.kb_delete(entry_id):
        raise HTTPException(404, "Entry not found")


@router.get("/context")
def get_context():
    """Return all knowledge entries as a combined context string for Claude."""
    return {"context": db.kb_get_all_context()}


@router.get("/links")
def list_links(
    type: Literal["postman", "confluence"] = Query(...),
    sub_section: str = Query(""),
):
    return db.link_list(type, sub_section)


@router.post("/links", status_code=201)
def create_link(body: LinkEntry):
    return db.link_create(body.title, body.url, body.description, body.source_type, body.sub_section, body.collection_json)


@router.delete("/links/{link_id}", status_code=204)
def delete_link(link_id: int):
    if not db.link_delete(link_id):
        raise HTTPException(404, "Link not found")

function toggleHabit(btn) {
    const item = btn.closest('.habit-item');
    const habitName = item.dataset.habit;
    btn.disabled = true;

    fetch('/api/habits/toggle', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({habit_name: habitName})
    })
    .then(r => r.json())
    .then(data => {
        const done = data.action === 'done';
        btn.className = 'btn btn-sm habit-toggle-btn ' + (done ? 'btn-success' : 'btn-outline-primary');
        btn.innerHTML = done ? '<i class="bi bi-check-lg"></i> 완료' : '기록하기';
        item.querySelector('.streak-badge').textContent = '🔥 ' + data.streak + '일 연속';
        item.querySelector('.total-badge').textContent = '총 ' + data.total + '일';
        const bars = item.querySelectorAll('.habit-bar');
        data.days.forEach((day, i) => {
            bars[i].className = 'rounded-top habit-bar ' +
                (day.done ? 'bg-primary' : 'bg-secondary bg-opacity-25') +
                (day.is_today ? ' habit-bar-today' : '');
            bars[i].style.height = day.done ? '28px' : '8px';
        });
        btn.disabled = false;
    })
    .catch(() => { btn.disabled = false; });
}

function setBtnFeedback(btn, state, msg) {
    if (!btn._orig) btn._orig = {html: btn.innerHTML, cls: btn.className};
    clearTimeout(btn._timer);
    if (state === 'loading') {
        btn.disabled = true;
        btn.innerHTML = '<span class="spinner-border spinner-border-sm me-1" role="status"></span>저장 중...';
    } else if (state === 'success') {
        btn.disabled = false;
        btn.className = btn._orig.cls.replace(/\bbtn-(primary|secondary|outline-\w+)\b/, 'btn-success');
        btn.innerHTML = '✓ ' + (msg || '저장됨');
        btn._timer = setTimeout(() => {
            btn.className = btn._orig.cls; btn.innerHTML = btn._orig.html; btn._orig = null;
        }, 1800);
    } else if (state === 'error') {
        btn.disabled = false;
        btn.className = btn._orig.cls.replace(/\bbtn-(primary|secondary|outline-\w+)\b/, 'btn-danger');
        btn.innerHTML = msg || '오류';
        btn._timer = setTimeout(() => {
            btn.className = btn._orig.cls; btn.innerHTML = btn._orig.html; btn._orig = null;
        }, 2500);
    }
}

(async function initDashboardEditableModal() {
    const contactQuickRows = document.querySelectorAll('.quickview-contact-row');
    const entityQuickRows = document.querySelectorAll('.quickview-entity-row');
    const editContactModalEl = document.getElementById('editContactModal');
    const viewEntityModalEl = document.getElementById('viewEntityModal');
    const oppModalEl = document.getElementById('oppModal');

    if (!contactQuickRows.length && !entityQuickRows.length) return;
    if (!editContactModalEl || !viewEntityModalEl || !oppModalEl || typeof bootstrap === 'undefined') return;

    const style = document.createElement('style');
    style.textContent = '.quickview-row{cursor:pointer;} .quickview-row:focus-visible{outline:2px solid var(--bs-primary);outline-offset:-2px;}';
    document.head.appendChild(style);

    const _ld = window.__LANDING_DATA || {};
    const contactSeed = _ld.contactSeed || [];
    const entitySeed = _ld.entitySeed || [];
    const contactsById = new Map();
    const entitiesById = new Map();
    let currentEntityHmac = null;

    const editContactModal = bootstrap.Modal.getOrCreateInstance(editContactModalEl);
    const viewEntityModal = bootstrap.Modal.getOrCreateInstance(viewEntityModalEl);
    const oppModal = bootstrap.Modal.getOrCreateInstance(oppModalEl);

    function normalizeDateForInput(val) {
        if (!val) return '';
        if (/^\d{4}-\d{2}$/.test(val)) return `${val}-01`;
        if (/^\d{4}$/.test(val)) return `${val}-01-01`;
        return val;
    }

    function indexContacts(items) {
        (items || []).forEach((contact) => {
            if (contact && contact.name_hmac) contactsById.set(contact.name_hmac, contact);
        });
    }

    function indexEntities(items) {
        (items || []).forEach((entity) => {
            if (entity && entity.entity_hmac) entitiesById.set(entity.entity_hmac, entity);
        });
    }

    async function refreshContacts() {
        try {
            const res = await fetch('/api/contacts');
            const data = await res.json();
            if (data.error) return false;
            indexContacts(data.contacts || []);
            return true;
        } catch (_e) {
            return false;
        }
    }

    async function refreshEntities() {
        try {
            const res = await fetch('/api/entities');
            const data = await res.json();
            if (data.error) return false;
            indexEntities(data.entities || []);
            return true;
        } catch (_e) {
            return false;
        }
    }

    const _embeddedTags = _ld.validTags || [];
    function loadTags() {
        const tags = _embeddedTags;
        ['editTag', 'editEntityTag'].forEach((id) => {
            const sel = document.getElementById(id);
            if (!sel) return;
            const current = sel.value;
            sel.innerHTML = '<option value="">선택</option>' + tags.map((t) => `<option value="${t}">${t}</option>`).join('');
            sel.value = current;
        });
    }

    function updateUrlState(modalType, id, mode) {
        const url = new URL(window.location.href);
        if (modalType && id) {
            url.searchParams.set('modal', modalType);
            url.searchParams.set('id', id);
        } else {
            url.searchParams.delete('modal');
            url.searchParams.delete('id');
        }

        if (mode === 'push') {
            window.history.pushState({}, '', url);
            return;
        }
        window.history.replaceState({}, '', url);
    }

    async function ensureContact(nameHmac) {
        if (contactsById.has(nameHmac)) return contactsById.get(nameHmac);
        const ok = await refreshContacts();
        if (!ok) return null;
        return contactsById.get(nameHmac) || null;
    }

    async function ensureEntity(entityHmac) {
        if (entitiesById.has(entityHmac)) return entitiesById.get(entityHmac);
        const ok = await refreshEntities();
        if (!ok) return null;
        return entitiesById.get(entityHmac) || null;
    }

    async function openContactEdit(nameHmac, historyMode) {
        const contact = await ensureContact(nameHmac);
        if (!contact) return false;

        document.getElementById('editNameHmac').value = nameHmac;
        document.getElementById('editName').value = contact.name || '';
        document.getElementById('editCP').value = contact.contact_priority || '';
        document.getElementById('editEmployer').value = contact.employer || '';
        document.getElementById('editTitle').value = contact.title || '';
        document.getElementById('editFU').value = contact.follow_up_priority || 'FU9';
        document.getElementById('editFUDate').value = normalizeDateForInput(contact.follow_up_date || '');
        document.getElementById('editLastContact').value = normalizeDateForInput(contact.last_contact || '');
        document.getElementById('editFUNote').value = contact.follow_up_note || '';
        document.getElementById('editEmail').value = contact.email || '';
        document.getElementById('editPhone').value = contact.phone || '';
        document.getElementById('editTag').value = contact.tag || '';
        document.getElementById('editReferredBy').value = contact.referred_by || '';
        document.getElementById('editKVI').value = contact.key_value_interest || '';

        const logsDiv = document.getElementById('interactionLogs');
        logsDiv.innerHTML = '로딩 중...';
        try {
            const res = await fetch(`/api/contacts/${nameHmac}/logs`);
            const data = await res.json();
            const logs = data.logs || [];
            if (!logs.length) {
                logsDiv.innerHTML = '<p class="text-muted">기록 없음</p>';
            } else {
                logsDiv.innerHTML = logs.map((l) => `
                    <div class="border-bottom py-2">
                        <strong>${l.date}</strong> — ${l.context}
                        ${l.key_value_extracted ? `<br><small class="text-primary">Key: ${l.key_value_extracted}</small>` : ''}
                    </div>
                `).join('');
            }
        } catch (_e) {
            logsDiv.innerHTML = '<p class="text-danger">로그 로딩 실패</p>';
        }

        viewEntityModal.hide();
        editContactModal.show();
        if (historyMode) updateUrlState('contact', nameHmac, historyMode);
        return true;
    }

    async function loadSuggestedContacts(entityHmac, relatedRaw) {
        const area = document.getElementById('relatedIndividualsArea');
        area.innerHTML = '<small class="text-muted">로딩 중...</small>';

        try {
            const res = await fetch(`/api/entities/${entityHmac}/suggested-contacts`);
            const data = await res.json();
            const relatedSet = new Set((relatedRaw || '').split(',').map((s) => s.trim()).filter(Boolean));

            function renderContact(c) {
                const isChecked = relatedSet.has(c.name_hmac);
                const badgeHtml = c.match_reason
                    ? `<span class="badge bg-info text-dark ms-1" style="font-size:0.7em">${c.match_reason}</span>`
                    : '';
                return `<div class="form-check">
                    <input class="form-check-input related-check" type="checkbox" value="${c.name_hmac}" id="rc_${c.name_hmac}" ${isChecked ? 'checked' : ''}>
                    <label class="form-check-label" for="rc_${c.name_hmac}">${c.display_name}${badgeHtml}</label>
                </div>`;
            }

            let html = '';
            if (data.suggested && data.suggested.length) {
                html += '<div class="mb-2"><small class="text-muted fw-semibold">추천 (Employer 매칭)</small></div>';
                html += data.suggested.map(renderContact).join('');
            }
            if (data.others && data.others.length) {
                if (html) html += '<hr class="my-2">';
                html += '<details><summary class="small text-muted" style="cursor:pointer">기타 연락처 더보기</summary><div class="mt-1">';
                html += data.others.map(renderContact).join('');
                html += '</div></details>';
            }

            area.innerHTML = html || '<small class="text-muted">연락처 없음</small>';
            area.querySelectorAll('.related-check').forEach((cb) => {
                cb.addEventListener('change', updateRelatedHidden);
            });
        } catch (_e) {
            area.innerHTML = '<small class="text-danger">로드 실패</small>';
        }
    }

    function updateRelatedHidden() {
        const checked = Array.from(document.querySelectorAll('#relatedIndividualsArea .related-check:checked')).map((cb) => cb.value);
        document.getElementById('editEntityRelated').value = checked.join(', ');
    }

    async function loadOpportunities(entityHmac) {
        const listDiv = document.getElementById('opportunitiesList');
        listDiv.innerHTML = '<p class="text-muted small">로딩 중...</p>';

        try {
            const res = await fetch(`/api/entities/${entityHmac}/opportunities`);
            const data = await res.json();
            const opps = data.opportunities || [];

            if (!opps.length) {
                listDiv.innerHTML = '<p class="text-muted small">등록된 Opportunity가 없습니다.</p>';
                return;
            }

            listDiv.innerHTML = opps.map((o) => `
                <div class="border rounded p-2 mb-2">
                    <div class="d-flex justify-content-between align-items-start">
                        <div class="fw-semibold">${o.title}</div>
                        <div>
                            <button class="btn btn-link btn-sm p-0 me-2 text-secondary"
                                onclick="openEditOpp('${entityHmac}', '${o.opp_id}', ${JSON.stringify(o.title).replace(/"/g, '&quot;')}, ${JSON.stringify(o.details || '').replace(/"/g, '&quot;')})">
                                <i class="bi bi-pencil"></i>
                            </button>
                            <button class="btn btn-link btn-sm p-0 text-danger"
                                onclick="deleteOpp('${entityHmac}', '${o.opp_id}')">
                                <i class="bi bi-x-lg"></i>
                            </button>
                        </div>
                    </div>
                    ${o.details ? `<div class="text-muted small mt-1">${o.details}</div>` : ''}
                    <div class="text-muted" style="font-size:0.75em">${o.created_date || ''}</div>
                </div>
            `).join('');
        } catch (_e) {
            listDiv.innerHTML = '<p class="text-danger small">로드 실패</p>';
        }
    }

    async function loadEntityLogs(entityHmac) {
        const logsDiv = document.getElementById('entityInteractionLogs');
        logsDiv.innerHTML = '로딩 중...';
        try {
            const res = await fetch(`/api/entities/${entityHmac}/logs`);
            const data = await res.json();
            const logs = data.logs || [];
            if (!logs.length) {
                logsDiv.innerHTML = '<p class="text-muted">기록 없음</p>';
            } else {
                logsDiv.innerHTML = logs.map((l) => `
                    <div class="border-bottom py-2">
                        <strong>${l.date}</strong> — ${l.context}
                        ${l.key_value_extracted ? `<br><small class="text-primary">Key: ${l.key_value_extracted}</small>` : ''}
                    </div>
                `).join('');
            }
        } catch (_e) {
            logsDiv.innerHTML = '<p class="text-danger">로그 로딩 실패</p>';
        }
    }

    async function openEntityEdit(entityHmac, historyMode) {
        const entity = await ensureEntity(entityHmac);
        if (!entity) return false;

        currentEntityHmac = entityHmac;

        document.getElementById('viewEntityTitle').textContent = entity.name || '엔티티';
        document.getElementById('editEntityHmac').value = entityHmac;
        document.getElementById('editEntityBP').value = entity.business_priority || '';
        document.getElementById('editEntityFU').value = entity.follow_up_priority || 'FU9';
        document.getElementById('editEntityFUDate').value = normalizeDateForInput(entity.follow_up_date || '');
        document.getElementById('editEntityLastContact').value = normalizeDateForInput(entity.last_contact || '');
        document.getElementById('editEntityFUNote').value = entity.follow_up_note || '';
        document.getElementById('editEntityKVI').value = entity.key_value_interest || '';
        document.getElementById('editEntityReferredBy').value = entity.referred_by || '';
        document.getElementById('editEntityAssignee').value = entity.assignee || '';
        document.getElementById('editEntityTag').value = entity.tag || '';
        document.getElementById('editEntityRelated').value = entity.related_individuals || '';

        await Promise.all([
            loadSuggestedContacts(entityHmac, entity.related_individuals || ''),
            loadOpportunities(entityHmac),
            loadEntityLogs(entityHmac),
        ]);

        editContactModal.hide();
        viewEntityModal.show();
        if (historyMode) updateUrlState('entity', entityHmac, historyMode);
        return true;
    }

    async function syncModalWithUrl() {
        const params = new URLSearchParams(window.location.search);
        const modalType = params.get('modal');
        const id = params.get('id');

        if (modalType === 'contact' && id) {
            if (!await openContactEdit(id, null)) {
                editContactModal.hide();
                viewEntityModal.hide();
                updateUrlState(null, null, 'replace');
            }
            return;
        }
        if (modalType === 'entity' && id) {
            if (!await openEntityEdit(id, null)) {
                editContactModal.hide();
                viewEntityModal.hide();
                updateUrlState(null, null, 'replace');
            }
            return;
        }

        editContactModal.hide();
        viewEntityModal.hide();
    }

    function bindQuickRows(rows, key, opener) {
        rows.forEach((row) => {
            const itemId = row.dataset[key];
            if (!itemId) return;

            row.addEventListener('click', async (event) => {
                if (event.target.closest('a, button, input, select, textarea')) return;
                await opener(itemId, 'push');
            });
            row.addEventListener('keydown', async (event) => {
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                await opener(itemId, 'push');
            });
        });
    }

    document.getElementById('updateContactBtn').addEventListener('click', async () => {
        const btn = document.getElementById('updateContactBtn');
        const nameHmac = document.getElementById('editNameHmac').value;
        const fields = ['contact_priority', 'employer', 'title', 'follow_up_priority', 'follow_up_date', 'follow_up_note', 'last_contact', 'email', 'phone', 'tag', 'referred_by', 'key_value_interest'];
        const form = document.getElementById('editContactForm');
        const formData = new FormData(form);
        const data = {};
        fields.forEach((f) => {
            const val = formData.get(f);
            if (val !== null) data[f] = val;
        });

        setBtnFeedback(btn, 'loading');
        try {
            const res = await fetch(`/api/contacts/${nameHmac}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data),
            });
            const result = await res.json();
            if (result.error) {
                setBtnFeedback(btn, 'error', result.errors ? result.errors[0] : result.error);
                return;
            }
            setBtnFeedback(btn, 'success', '저장됨');
            setTimeout(() => { editContactModal.hide(); refreshContacts(); }, 600);
        } catch (e) {
            setBtnFeedback(btn, 'error', '오류');
        }
    });

    document.getElementById('deleteContactBtn').addEventListener('click', async () => {
        if (!confirm('휴지통으로 이동하시겠습니까?')) return;
        const nameHmac = document.getElementById('editNameHmac').value;
        try {
            const res = await fetch(`/api/contacts/${nameHmac}`, { method: 'DELETE' });
            const result = await res.json();
            if (result.error) {
                alert(result.error);
                return;
            }
            editContactModal.hide();
            window.location.reload();
        } catch (e) {
            alert('오류: ' + e.message);
        }
    });

    document.getElementById('updateEntityBtn').addEventListener('click', async () => {
        const btn = document.getElementById('updateEntityBtn');
        const entityHmac = document.getElementById('editEntityHmac').value;
        const fields = ['business_priority', 'follow_up_priority', 'follow_up_date', 'follow_up_note', 'last_contact', 'tag', 'referred_by', 'assignee', 'key_value_interest', 'related_individuals'];
        const form = document.getElementById('editEntityForm');
        const formData = new FormData(form);
        const data = {};
        fields.forEach((f) => {
            const val = formData.get(f);
            if (val !== null) data[f] = val;
        });

        setBtnFeedback(btn, 'loading');
        try {
            const res = await fetch(`/api/entities/${entityHmac}`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(data),
            });
            const result = await res.json();
            if (result.error) {
                setBtnFeedback(btn, 'error', result.error);
                return;
            }
            setBtnFeedback(btn, 'success', '저장됨');
            setTimeout(() => { viewEntityModal.hide(); refreshEntities(); }, 600);
        } catch (e) {
            setBtnFeedback(btn, 'error', '오류');
        }
    });

    document.getElementById('deleteEntityBtn').addEventListener('click', async () => {
        if (!confirm('휴지통으로 이동하시겠습니까?')) return;
        const entityHmac = document.getElementById('editEntityHmac').value;
        try {
            const res = await fetch(`/api/entities/${entityHmac}`, { method: 'DELETE' });
            const result = await res.json();
            if (result.error) {
                alert(result.error);
                return;
            }
            viewEntityModal.hide();
            window.location.reload();
        } catch (e) {
            alert('오류: ' + e.message);
        }
    });

    document.getElementById('addOppBtn').addEventListener('click', () => {
        document.getElementById('oppModalTitle').textContent = 'Opportunity 추가';
        document.getElementById('oppEntityHmac').value = currentEntityHmac || '';
        document.getElementById('oppId').value = '';
        document.getElementById('oppTitle').value = '';
        document.getElementById('oppDetails').value = '';
        oppModal.show();
    });

    window.openEditOpp = function openEditOpp(entityHmac, oppId, title, details) {
        document.getElementById('oppModalTitle').textContent = 'Opportunity 수정';
        document.getElementById('oppEntityHmac').value = entityHmac;
        document.getElementById('oppId').value = oppId;
        document.getElementById('oppTitle').value = title || '';
        document.getElementById('oppDetails').value = details || '';
        oppModal.show();
    };

    window.deleteOpp = async function deleteOpp(entityHmac, oppId) {
        if (!confirm('삭제하시겠습니까?')) return;
        try {
            const res = await fetch(`/api/entities/${entityHmac}/opportunities/${oppId}`, { method: 'DELETE' });
            const result = await res.json();
            if (result.error) {
                alert(result.error);
                return;
            }
            await loadOpportunities(entityHmac);
        } catch (e) {
            alert('오류: ' + e.message);
        }
    };

    document.getElementById('saveOppBtn').addEventListener('click', async (event) => {
        const btn = event.currentTarget;
        if (btn.disabled) return;

        const entityHmac = document.getElementById('oppEntityHmac').value;
        const oppId = document.getElementById('oppId').value;
        const title = document.getElementById('oppTitle').value.trim();
        const details = document.getElementById('oppDetails').value.trim();

        if (!title) {
            setBtnFeedback(btn, 'error', 'Title 필수');
            return;
        }

        setBtnFeedback(btn, 'loading');
        try {
            let res;
            if (oppId) {
                res = await fetch(`/api/entities/${entityHmac}/opportunities/${oppId}`, {
                    method: 'PUT',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title, details}),
                });
            } else {
                res = await fetch(`/api/entities/${entityHmac}/opportunities`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({title, details}),
                });
            }
            const result = await res.json();
            if (result.error) {
                setBtnFeedback(btn, 'error', result.error);
                return;
            }
            setBtnFeedback(btn, 'success', '저장됨');
            setTimeout(() => { oppModal.hide(); loadOpportunities(entityHmac); }, 600);
        } catch (e) {
            setBtnFeedback(btn, 'error', '오류');
        }
    });

    bindQuickRows(contactQuickRows, 'contactId', openContactEdit);
    bindQuickRows(entityQuickRows, 'entityId', openEntityEdit);

    editContactModalEl.addEventListener('hidden.bs.modal', () => {
        const params = new URLSearchParams(window.location.search);
        if (params.get('modal') === 'contact') updateUrlState(null, null, 'replace');
    });
    viewEntityModalEl.addEventListener('hidden.bs.modal', () => {
        const params = new URLSearchParams(window.location.search);
        if (params.get('modal') === 'entity') updateUrlState(null, null, 'replace');
    });

    window.addEventListener('popstate', () => {
        syncModalWithUrl();
    });

    indexContacts(contactSeed);
    indexEntities(entitySeed);
    loadTags();
    await syncModalWithUrl();
})();

async function familyToggle(habitName, idx, event) {
    const dateVal = document.getElementById(`family-date-${idx}`).value;
    const btn = event.target;
    setBtnFeedback(btn, 'loading');
    try {
        const res = await fetch('/api/habits/toggle', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({habit_name: habitName, date: dateVal || undefined})
        });
        const data = await res.json();
        setBtnFeedback(btn, 'success', data.action === 'done' ? '완료' : '취소됨');
        if (data.last_date) {
            document.getElementById(`family-last-${idx}`).innerHTML =
                `<strong>${data.last_date}</strong>`;
        }
    } catch(e) {
        setBtnFeedback(btn, 'error', '오류');
    }
}

// --- Anki Review Modal ---
(function() {
    let _cards = [], _idx = 0, _reviewed = 0, _flipped = false;

    const modal = document.getElementById('ankiReviewModal');
    if (!modal) return;

    modal.addEventListener('show.bs.modal', async () => {
        _cards = []; _idx = 0; _reviewed = 0;
        document.getElementById('ankiModalDone').style.display = 'none';
        document.getElementById('ankiModalReview').style.display = '';
        document.getElementById('ankiModalProgress').style.width = '0%';

        try {
            const res = await fetch('/api/anki/due');
            _cards = await res.json();
        } catch(e) { _cards = []; }

        _ankiShow();
    });

    window.ankiModalFlip = function() {
        if (_flipped) return;
        _flipped = true;
        document.getElementById('ankiModalFront').style.display = 'none';
        document.getElementById('ankiModalBack').style.display = '';
        document.getElementById('ankiModalHint').style.display = 'none';
        document.getElementById('ankiModalRating').style.removeProperty('display');
    };

    window.ankiModalRate = async function(rating) {
        const card = _cards[_idx];
        document.querySelectorAll('.anki-rate-btn').forEach(b => b.disabled = true);
        try {
            await fetch(`/api/anki/cards/${card.id}/review`, {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({rating})
            });
        } catch(e) {}
        _reviewed++;
        _idx++;
        document.querySelectorAll('.anki-rate-btn').forEach(b => b.disabled = false);
        _ankiShow();
    };

    window.ankiModalDelete = async function() {
        if (!confirm('이 카드를 완전히 삭제하시겠습니까?')) return;
        const card = _cards[_idx];
        try {
            await fetch(`/api/anki/cards/${card.id}`, { method: 'DELETE' });
        } catch(e) {}
        _cards.splice(_idx, 1);
        _ankiShow();
    };

    function _ankiShow() {
        if (_idx >= _cards.length) { _ankiDone(); return; }
        _flipped = false;
        const card = _cards[_idx];
        const total = _cards.length;
        const pct = Math.round((_idx / total) * 100);

        const isHighlight = card.card_type === 'highlight';
        const header = document.getElementById('ankiModalHighlightHeader');
        const srcEl  = document.getElementById('ankiModalHighlightSource');

        document.getElementById('ankiModalFront').textContent = card.front;
        document.getElementById('ankiModalBack').textContent  = card.back;

        if (isHighlight) {
            srcEl.textContent = card.front + (card.source_ref ? '  ·  ' + card.source_ref : '');
            header.style.display = '';
            document.getElementById('ankiModalFront').style.display = 'none';
            document.getElementById('ankiModalBack').style.display  = '';
            document.getElementById('ankiModalHint').style.display  = 'none';
            document.getElementById('ankiModalCard').style.cursor   = 'default';
            document.getElementById('ankiModalCard').onclick        = null;
            document.getElementById('ankiModalRating').style.removeProperty('display');
            _flipped = true;
        } else {
            header.style.display = 'none';
            document.getElementById('ankiModalFront').style.display = '';
            document.getElementById('ankiModalBack').style.display  = 'none';
            document.getElementById('ankiModalHint').style.display  = '';
            document.getElementById('ankiModalCard').style.cursor   = 'pointer';
            document.getElementById('ankiModalCard').onclick        = ankiModalFlip;
            document.getElementById('ankiModalRating').style.display = 'none';
        }

        document.getElementById('ankiModalProgress').style.width = pct + '%';
        document.getElementById('ankiModalRemaining').textContent =
            (total - _idx) + ' / ' + total;
    }

    function _ankiDone() {
        document.getElementById('ankiModalProgress').style.width = '100%';
        document.getElementById('ankiModalRemaining').textContent = '완료!';
        document.getElementById('ankiModalReview').style.display = 'none';
        document.getElementById('ankiModalDone').style.display = '';
        document.getElementById('ankiModalDoneMsg').textContent =
            `오늘 ${_reviewed}개 카드를 복습했습니다.`;

        setTimeout(() => {
            const m = bootstrap.Modal.getInstance(modal);
            if (m) m.hide();
            const strip = document.querySelector('.anki-strip');
            if (strip) {
                strip.classList.add('anki-strip--done');
                strip.style.borderLeftColor = '#22c55e';
                strip.innerHTML = `
                    <div>
                        <div class="anki-done-text">
                            <i class="bi bi-check-circle-fill"></i> All caught up for today
                        </div>
                        <div class="anki-done-sub">
                            다음 복습은 내일 — <a href="/anki" class="text-muted">덱 보기</a>
                        </div>
                    </div>`;
            }
        }, 1500);
    }
})();

// --- Compliment Modal ---
document.getElementById('saveComplimentBtn').addEventListener('click', async function() {
    const recipient = document.getElementById('complimentRecipient').value.trim();
    const content = document.getElementById('complimentContent').value.trim();
    const given_at = document.getElementById('complimentDate').value;
    if (!recipient || !content) {
        alert('칭찬받은 사람과 내용을 입력해 주세요.');
        return;
    }
    const btn = this;
    btn.disabled = true;
    btn.textContent = '저장 중...';
    try {
        const res = await fetch('/api/compliments', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({recipient, content, given_at})
        });
        if (res.ok) {
            bootstrap.Modal.getInstance(document.getElementById('addComplimentModal')).hide();
            document.getElementById('complimentRecipient').value = '';
            document.getElementById('complimentContent').value = '';
            location.reload();
        } else {
            const err = await res.json();
            alert(err.error || '저장 실패');
        }
    } catch(e) {
        alert('오류가 발생했습니다.');
    } finally {
        btn.disabled = false;
        btn.textContent = '저장';
    }
});

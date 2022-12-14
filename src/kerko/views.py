import copy
import math
import time
from datetime import datetime

from babel.numbers import format_decimal
from flask import (abort, current_app, flash, make_response, redirect,
                   render_template, request, send_from_directory, url_for)
from flask_babel import get_locale, gettext, ngettext

from . import babel_domain, blueprint, meta, query
from .breadbox import build_breadbox
from .criteria import Criteria
from .exceptions import except_abort
from .forms import SearchForm
from .pager import build_pager, get_page_numbers
from .pager import get_sections as get_pager_sections
from .sorter import build_sorter
from .storage import (SchemaError, SearchIndexError, get_doc_count,
                      get_storage_dir)

SITEMAP_URL_MAX_COUNT = 50000


@blueprint.route('/', methods=['GET', 'POST'])
@except_abort(SchemaError, 500)
@except_abort(SearchIndexError, 503)
def search():
    """View the results of a search."""
    start_time = time.process_time()

    if current_app.config['KERKO_USE_TRANSLATIONS']:
        babel_domain.as_default()

    criteria = Criteria(request)
    form = SearchForm(csrf_enabled=False)
    if form.validate_on_submit():
        url = criteria.build_add_keywords_url(
            scope=form.scope.data,
            value=form.keywords.data)
        return redirect(url, 302)

    base_filter_terms = query.build_filter_terms('item_type', exclude=['note', 'attachment'])
    search_results, facet_results, total_count, page_count, last_sync = query.run_query(
        criteria,
        query.get_search_return_fields(criteria.page_len),
        default_terms=base_filter_terms,
    )

    if criteria.page_len == 1 and criteria.id and (
            total_count == 0 or criteria.id != search_results[0]['id']
    ):
        # The search result page no longer points to the desired item.
        return redirect(url_for('.item_view', item_id=criteria.id, _external=True), 301)

    criteria.fit_pager(page_count)
    breadbox = build_breadbox(criteria, facet_results)
    pager_sections = get_pager_sections(criteria.page_num, page_count)
    context = {
        'facet_results': facet_results,
        'breadbox': breadbox,
        'active_facets': breadbox['filters'].keys() if 'filters' in breadbox else [],
        'sorter': build_sorter(criteria),
        'total_count': total_count,
        'total_count_formatted': format_decimal(total_count, locale=get_locale()),
        'page_count': page_count,
        'page_count_formatted': format_decimal(page_count, locale=get_locale()),
        'page_len': criteria.page_len,
        'show_abstracts': criteria.show_abstracts,
        'abstracts_toggler_url': criteria.build_url(
            show_abstracts=not criteria.show_abstracts, page_num=criteria.page_num
        ),
        'is_searching': criteria.has_keyword_search() or criteria.has_filter_search(),
        'locale': get_locale(),
        'last_sync': datetime.fromtimestamp(
            last_sync, tz=datetime.now().astimezone().tzinfo
        ) if last_sync else None,
    }

    if criteria.page_len == 1 and total_count != 0:
        # Retrieve item ids corresponding to individual result page numbers.
        page_kwargs = {}
        page_criteria = copy.deepcopy(criteria)
        for page_num in get_page_numbers(pager_sections):
            if page_num == criteria.page_num:
                # We already know the current page's item id. No further query necessary.
                page_kwargs[page_num] = {'id_': search_results[0]['id']}
            else:
                # Run a search query to get the item id corresponding to the page number.
                page_criteria.page_num = page_num
                page_search_results, _, _, _, _ = query.run_query(
                    page_criteria,
                    return_fields=['id'],
                    query_facets=False,
                    default_terms=base_filter_terms,
                )
                if page_search_results:
                    page_kwargs[page_num] = {'id_': page_search_results[0]['id']}
        context['pager'] = build_pager(pager_sections, criteria, page_kwargs)

        list_page_num = int((criteria.page_num - 1) / current_app.config['KERKO_PAGE_LEN'] + 1)
        query.build_creators_display(search_results[0])
        query.build_item_facet_results(search_results[0])
        query.build_relations(
            search_results[0],
            query.get_search_return_fields(page_len=None, exclude=['coins']),
            sort=current_app.config['KERKO_RELATIONS_SORT'],
            default_terms=base_filter_terms,
        )
        if context['is_searching']:
            context['search_title'] = gettext('Your search')
        else:
            context['search_title'] = gettext('Full bibliography')
        return render_template(
            current_app.config['KERKO_TEMPLATE_SEARCH_ITEM'],
            title=search_results[0].get('data', {}).get('title', ''),
            item=search_results[0],
            item_url=url_for(
                '.item_view', item_id=search_results[0]['id'], _external=True
            ) if search_results[0] else '',
            highwirepress_tags=meta.build_highwirepress_tags(search_results[0]),
            back_url=criteria.build_url(page_num=list_page_num),
            time=time.process_time() - start_time,
            **context
        )

    if total_count > 0:
        context['pager'] = build_pager(pager_sections, criteria)
        search_results_urls = [
            criteria.build_url(
                page_num=(criteria.page_num - 1) * (criteria.page_len or 0) + i + 1,
                page_len=1,
                id_=result['id'],
            ) for i, result in enumerate(search_results)
        ]
        search_results = zip(search_results, search_results_urls)
        if context['is_searching']:
            context['title'] = ngettext('Result', 'Results', total_count)
        else:
            context['title'] = gettext('Full bibliography')
    else:
        context['title'] = gettext('Your search did not match any resources')
    return render_template(
        current_app.config['KERKO_TEMPLATE_SEARCH'],
        form=form,
        search_results=search_results,
        print_url=criteria.build_url(page_len='all', print_preview=True),
        print_preview=criteria.print_preview,
        download_urls={
            key: criteria.build_download_url(key)
            for key in current_app.config['KERKO_COMPOSER'].citation_formats.keys()
        },
        time=time.process_time() - start_time,
        **context
    )


@blueprint.route('/<path:item_id>')
@except_abort(SchemaError, 500)
@except_abort(SearchIndexError, 503)
def item_view(item_id):
    """View a full bibliographic record."""
    start_time = time.process_time()

    if current_app.config['KERKO_USE_TRANSLATIONS']:
        babel_domain.as_default()

    base_filter_terms = query.build_filter_terms('item_type', exclude=['note', 'attachment'])
    item, fellback = query.run_query_unique_with_fallback(
        ['id', 'alternate_id'],
        item_id,
        default_terms=base_filter_terms,
    )
    if not item:
        return abort(404)
    item_url = url_for('.item_view', item_id=item['id'], _external=True)
    if fellback:
        return redirect(item_url, 301)

    query.build_creators_display(item)
    query.build_item_facet_results(item)
    query.build_relations(
        item,
        query.get_search_return_fields(page_len=None, exclude=['coins']),
        sort=current_app.config['KERKO_RELATIONS_SORT'],
        default_terms=base_filter_terms,
    )
    return render_template(
        current_app.config['KERKO_TEMPLATE_ITEM'],
        title=item.get('data', {}).get('title', ''),
        item=item,
        item_url=item_url,
        highwirepress_tags=meta.build_highwirepress_tags(item),
        time=time.process_time() - start_time,
        locale=get_locale(),
    )


@blueprint.route('/<path:item_id>/download/<string:attachment_id>/')
@blueprint.route('/<path:item_id>/download/<string:attachment_id>/<string:attachment_filename>')
@except_abort(SchemaError, 500)
@except_abort(SearchIndexError, 503)
def child_attachment_download(item_id, attachment_id, attachment_filename=None):
    """
    Download a child attachment.

    If the URL does not specify the attachment's filename or provides the wrong
    filename, a redirect is performed to a corrected URL so that the client gets
    a proper filename.
    """
    if current_app.config['KERKO_USE_TRANSLATIONS']:
        babel_domain.as_default()

    item, fellback = query.run_query_unique_with_fallback(
        ['id', 'alternate_id'],
        item_id,
        default_terms=query.build_filter_terms('item_type', exclude=['note', 'attachment']),
    )
    if not item:
        return abort(404)

    matching_attachments = list(
        filter(lambda a: a.get('id') == attachment_id, item.get('attachments', []))
    )
    if not matching_attachments or len(matching_attachments) > 1:
        flash(
            gettext(
                "The document you have requested has been removed. "
                "Please check below for the latest documents available."
            ), 'warning'
        )
        return redirect(url_for('.item_view', item_id=item['id']), 301)
    attachment = matching_attachments[0]

    filepath = get_storage_dir('attachments') / attachment_id
    if not filepath.exists():
        return abort(404)

    filename = attachment['data'].get('filename', item['id'])
    if fellback or attachment_filename != filename:
        return redirect(url_for(
            '.child_attachment_download',
            item_id=item['id'],
            attachment_id=attachment_id,
            attachment_filename=filename,
        ), 301)

    return send_from_directory(
        get_storage_dir('attachments'),
        attachment_id,
        download_name=filename,
        mimetype=attachment['data'].get('contentType', 'octet-stream'),
    )


@blueprint.route('/download/<string:item_id>/')
@blueprint.route('/download/<string:item_id>/<string:attachment_filename>')
@except_abort(SchemaError, 500)
@except_abort(SearchIndexError, 503)
def standalone_attachment_download(item_id, attachment_filename=None):
    """
    Download a standalone attachment.

    If the URL does not specify the attachment's filename or provides the wrong
    filename, a redirect is performed to a corrected URL so that the client gets
    a proper filename.
    """
    if current_app.config['KERKO_USE_TRANSLATIONS']:
        babel_domain.as_default()

    item, fellback = query.run_query_unique_with_fallback(
        ['id', 'alternate_id'],
        item_id,
        default_terms=query.build_filter_terms('item_type', include=['attachment']),
    )
    if not item:
        return abort(404)

    filepath = get_storage_dir('attachments') / item_id
    if not filepath.exists():
        return abort(404)

    filename = item['data'].get('filename', item['id'])
    if fellback or attachment_filename != filename:
        return redirect(url_for(
            '.standalone_attachment_download',
            item_id=item['id'],
            attachment_filename=filename,
        ), 301)

    return send_from_directory(
        get_storage_dir('attachments'),
        item_id,
        download_name=filename,
        mimetype=item['data'].get('contentType', 'octet-stream'),
    )


@blueprint.route('/<path:item_id>/export/<string:citation_format_key>')
@except_abort(SchemaError, 500)
@except_abort(SearchIndexError, 503)
def item_citation_download(item_id, citation_format_key):
    """Download a record."""
    if current_app.config['KERKO_USE_TRANSLATIONS']:
        babel_domain.as_default()

    item, fellback = query.run_query_unique_with_fallback(
        ['id', 'alternate_id'],
        item_id,
        default_terms=query.build_filter_terms('item_type', exclude=['note', 'attachment']),
    )
    if not item:
        return abort(404)

    citation_format = current_app.config['KERKO_COMPOSER'].citation_formats.get(citation_format_key)
    if not citation_format:
        return abort(404)

    content = item.get(citation_format.field.key)
    if not content:
        return abort(404)

    if fellback:
        return redirect(
            url_for(
                '.item_citation_download',
                item_id=item['id'],
                citation_format_key=citation_format_key
            ), 301
        )

    response = make_response(content)
    response.headers['Content-Disposition'] = \
        f"attachment; filename={item['id']}.{citation_format.extension}"
    response.headers['Content-Type'] = \
        f'{citation_format.mime_type}; charset=utf-8'
    return response


@blueprint.route('/export/<string:citation_format_key>/')
@except_abort(SchemaError, 500)
@except_abort(SearchIndexError, 503)
def search_citation_download(citation_format_key):
    """Download all records resulting from a search."""
    citation_format = current_app.config['KERKO_COMPOSER'].citation_formats.get(citation_format_key)
    if not citation_format:
        return abort(404)

    criteria = Criteria(request)
    criteria.page_len = None

    search_results, _, total_count, _, _ = query.run_query(  # TODO: Avoid building facet results.
        criteria,
        return_fields=[citation_format.field.key],
        default_terms=query.build_filter_terms('item_type', exclude=['note', 'attachment']),
    )

    if total_count == 0:
        return abort(404)

    citations = [result.get(citation_format.field.key, '') for result in search_results]
    response = make_response(
        citation_format.group_format.format(
            citation_format.group_item_delimiter.join(citations)
        )
    )
    response.headers['Content-Disposition'] = \
        f'attachment; filename=bibliography.{citation_format.extension}'  # TODO: Make filename configurable.
    response.headers['Content-Type'] = \
        f'{citation_format.mime_type}; charset=utf-8'
    return response


def get_sitemap_page_count():
    count = get_doc_count('index')
    if count:
        count = math.ceil(count / SITEMAP_URL_MAX_COUNT)
        # Note: count may include items that will be excluded from the
        # sitemap, but this is the efficient way to get a good enough count.
    return count


@blueprint.route('/sitemap.xml')
@except_abort(SearchIndexError, 503)
def sitemap_index():
    """Generate a sitemap index."""
    response = make_response(
        render_template(
            'kerko/sitemap_index.xml.jinja2',
            sitemap_count=get_sitemap_page_count(),
        )
    )
    response.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return response


@blueprint.route(f'/sitemap<int(min=1, max={SITEMAP_URL_MAX_COUNT}):page_num>.xml')
@except_abort(SchemaError, 500)
@except_abort(SearchIndexError, 503)
def sitemap(page_num):
    """Generate a sitemap."""
    if page_num > get_sitemap_page_count():
        # Return an empty sitemap rather than a 404. In the future, it might contain something.
        items = []
    else:
        base_filter_terms = query.build_filter_terms('item_type', exclude=['note', 'attachment'])
        items = query.run_query_filter_paged(
            page_num=page_num,
            page_len=SITEMAP_URL_MAX_COUNT,
            return_fields=['id', 'z_dateModified'],
            default_terms=base_filter_terms,
        )
    response = make_response(
        render_template(
            'kerko/sitemap.xml.jinja2',
            page_num=page_num,
            items=items,
        )
    )
    response.headers['Content-Type'] = 'application/xml; charset=utf-8'
    return response

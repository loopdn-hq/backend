from rest_framework.pagination import PageNumberPagination


class CompanyPagination(PageNumberPagination):
    """Ten a page: the Companies dialog loads ten, then ten more as it scrolls."""

    page_size = 10
    page_size_query_param = 'page_size'
    max_page_size = 50


class CampaignPagination(PageNumberPagination):
    """Twenty a page, newest first: the list shows a page and loads more."""

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100


class TransactionPagination(PageNumberPagination):
    """Twenty ledger rows a page, newest first."""

    page_size = 20
    page_size_query_param = 'page_size'
    max_page_size = 100

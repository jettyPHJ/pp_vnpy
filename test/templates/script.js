(function() {
    document.querySelectorAll('.filter-btn').forEach(function(btn) {
        btn.addEventListener('click', function(e) {
            var group = btn.closest('.section-card');
            group.querySelectorAll('.filter-btn').forEach(function(b) { b.classList.remove('active'); });
            btn.classList.add('active');
            
            var filter = btn.dataset.filter;
            group.querySelectorAll('tbody tr').forEach(function(tr) {
                if (filter === 'all') tr.style.display = '';
                else if (filter === 'limit') tr.style.display = (tr.dataset.type === 'limit') ? '' : 'none';
                else if (filter === 'stop') tr.style.display = (tr.dataset.type === 'stop') ? '' : 'none';
                else if (filter === 'abnormal') tr.style.display = (tr.dataset.abnormal === 'true') ? '' : 'none';
            });
        });
    });
})();

// 导航高亮
(function() {
    var sections = document.querySelectorAll('[data-section]');
    var navItems = document.querySelectorAll('.nav-item[href]');
    function onScroll() {
        var scrollY = window.scrollY + 80;
        var current = '';
        sections.forEach(function(s) {
            if (s.offsetTop <= scrollY) current = s.dataset.section;
        });
        navItems.forEach(function(n) {
            n.classList.toggle('active', n.getAttribute('href') === '#' + current);
        });
    }
    window.addEventListener('scroll', onScroll);
    onScroll();
})();